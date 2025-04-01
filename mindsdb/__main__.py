import os
import sys
import time
import atexit
import signal
import psutil
import asyncio
import secrets
import traceback
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, List

from packaging import version
from sqlalchemy.orm.attributes import flag_modified

from mindsdb.utilities import log

logger = log.getLogger("mindsdb")
logger.debug("Starting MindsDB...")

from mindsdb.__about__ import __version__ as mindsdb_version
from mindsdb.utilities.config import config
from mindsdb.utilities.exception import EntityNotExistsError
from mindsdb.utilities.starters import (
    start_http, start_mysql, start_mongo, start_postgres, start_ml_task_queue, start_scheduler, start_tasks,
    start_mcp
)
from mindsdb.utilities.ps import is_pid_listen_port, get_child_pids
from mindsdb.utilities.functions import get_versions_where_predictors_become_obsolete
from mindsdb.interfaces.database.integrations import integration_controller
from mindsdb.interfaces.database.projects import ProjectController
import mindsdb.interfaces.storage.db as db
from mindsdb.integrations.utilities.install import install_dependencies
from mindsdb.utilities.fs import clean_process_marks, clean_unlinked_process_marks
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.auth import register_oauth_client, get_aws_meta_data
from mindsdb.utilities.sentry import sentry_sdk  # noqa: F401

try:
    import torch.multiprocessing as mp
except Exception:
    import multiprocessing as mp
try:
    mp.set_start_method('spawn')
except RuntimeError:
    logger.info('Torch multiprocessing context already set, ignoring...')

_stop_event = threading.Event()


class TrunkProcessEnum(Enum):
    HTTP = 'http'
    MYSQL = 'mysql'
    MONGODB = 'mongodb'
    POSTGRES = 'postgres'
    JOBS = 'jobs'
    TASKS = 'tasks'
    ML_TASK_QUEUE = 'ml_task_queue'
    MCP = 'mcp'

    @classmethod
    def _missing_(cls, value):
        print(f'"{value}" is not a valid name of subprocess')
        sys.exit(1)


@dataclass
class TrunkProcessData:
    name: str
    entrypoint: Callable
    need_to_run: bool = False
    port: Optional[int] = None
    process: Optional[mp.Process] = None
    started: bool = False
    args: Optional[Tuple] = None
    restart_on_failure: bool = False
    max_restart_count: int = 3
    max_restart_interval_seconds: int = 60

    _restart_count: int = 0
    _restarts_time: List[int] = field(default_factory=list)

    def request_restart_attempt(self) -> bool:
        """Check if the process may be restarted.
        If `max_restart_count` == 0, then there are not restrictions on restarts count or interval.
        If `max_restart_interval_seconds` == 0, then there are no time limit for restarts count.

        Returns:
            bool: `True` if the number of restarts in the interval does not exceed
        """
        if self.max_restart_count == 0:
            return True
        current_time_seconds = int(time.time())
        self._restarts_time.append(current_time_seconds)
        if self.max_restart_interval_seconds > 0:
            self._restarts_time = [
                x for x in self._restarts_time
                if x >= (current_time_seconds - self.max_restart_interval_seconds)
            ]
        if len(self._restarts_time) > self.max_restart_count:
            return False
        return True

    @property
    def should_restart(self) -> bool:
        """In case of OOM we want to restart the process. OS kill the process with code 9 on linux when an OOM occurs.
        On other OS process will be restarted regardless the code.

        Returns:
            bool: `True` if the process need to be restarted on failure
        """
        if config.is_cloud:
            return False
        if sys.platform in ('linux', 'darwin'):
            return self.restart_on_failure and self.process.exitcode == -signal.SIGKILL.value
        else:
            if self.max_restart_count == 0:
                # to prevent infinity restarts, max_restart_count should be > 0
                logger.warning('In the current OS, it is not possible to use `max_restart_count=0`')
                return False
            return self.restart_on_failure


def close_api_gracefully(trunc_processes_struct):
    _stop_event.set()
    try:
        for trunc_processes_data in trunc_processes_struct.values():
            process = trunc_processes_data.process
            if process is None:
                continue
            try:
                childs = get_child_pids(process.pid)
                for p in childs:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except Exception:
                        p.kill()
                sys.stdout.flush()
                process.terminate()
                process.join()
                sys.stdout.flush()
            except psutil.NoSuchProcess:
                pass
    except KeyboardInterrupt:
        sys.exit(0)


def set_error_model_status_by_pids(unexisting_pids: List[int]):
    """Models have id of its traiing process in the 'training_metadata' field.
    If the pid does not exist, we should set the model status to "error".
    Note: only for local usage.

    Args:
        unexisting_pids (List[int]): list of 'pids' that do not exist.
    """
    predictor_records = (
        db.session.query(db.Predictor)
        .filter(
            db.Predictor.deleted_at.is_(None),
            db.Predictor.status.not_in([
                db.PREDICTOR_STATUS.COMPLETE,
                db.PREDICTOR_STATUS.ERROR
            ])
        )
        .all()
    )
    for predictor_record in predictor_records:
        predictor_process_id = (predictor_record.training_metadata or {}).get('process_id')
        if predictor_process_id in unexisting_pids:
            predictor_record.status = db.PREDICTOR_STATUS.ERROR
            if isinstance(predictor_record.data, dict) is False:
                predictor_record.data = {}
            if 'error' not in predictor_record.data:
                predictor_record.data['error'] = 'The training process was terminated for unknown reasons'
                flag_modified(predictor_record, 'data')
            db.session.commit()


def set_error_model_status_for_unfinished():
    """Set error status to any model if status not in 'complete' or 'error'
    Note: only for local usage.
    """
    predictor_records = (
        db.session.query(db.Predictor)
        .filter(
            db.Predictor.deleted_at.is_(None),
            db.Predictor.status.not_in([
                db.PREDICTOR_STATUS.COMPLETE,
                db.PREDICTOR_STATUS.ERROR
            ])
        )
        .all()
    )
    for predictor_record in predictor_records:
        predictor_record.status = db.PREDICTOR_STATUS.ERROR
        if isinstance(predictor_record.data, dict) is False:
            predictor_record.data = {}
        if 'error' not in predictor_record.data:
            predictor_record.data['error'] = 'Unknown error'
            flag_modified(predictor_record, 'data')
        db.session.commit()


def do_clean_process_marks():
    """delete unexisting 'process marks'
    """
    while _stop_event.wait(timeout=5) is False:
        unexisting_pids = clean_unlinked_process_marks()
        if not config.is_cloud and len(unexisting_pids) > 0:
            set_error_model_status_by_pids(unexisting_pids)


if __name__ == '__main__':
    # warn if less than 1Gb of free RAM
    if psutil.virtual_memory().available < (1 << 30):
        logger.warning(
            'The system is running low on memory. '
            + 'This may impact the stability and performance of the program.'
        )

    ctx.set_default()

    # ---- CHECK SYSTEM ----
    if not (sys.version_info[0] >= 3 and sys.version_info[1] >= 10):
        print("""
     MindsDB requires Python >= 3.10 to run

     Once you have supported Python version installed you can start mindsdb as follows:

     1. create and activate venv:
     python3 -m venv venv
     source venv/bin/activate

     2. install MindsDB:
     pip3 install mindsdb

     3. Run MindsDB
     python3 -m mindsdb

     More instructions in https://docs.mindsdb.com
         """)
        exit(1)

    if config.cmd_args.version:
        print(f'MindsDB {mindsdb_version}')
        sys.exit(0)

    config.raise_warnings(logger=logger)
    os.environ["MINDSDB_RUNTIME"] = "1"

    if os.environ.get("FLASK_SECRET_KEY") is None:
        os.environ["FLASK_SECRET_KEY"] = secrets.token_hex(32)

    if os.environ.get('ARROW_DEFAULT_MEMORY_POOL') is None:
        try:
            """It seems like snowflake handler have memory issue that related to pyarrow. Memory usage keep growing with
            requests. This is related to 'memory pool' that is 'mimalloc' by default: it is fastest but use a lot of ram
            """
            import pyarrow as pa
            try:
                pa.jemalloc_memory_pool()
                os.environ['ARROW_DEFAULT_MEMORY_POOL'] = 'jemalloc'
            except NotImplementedError:
                pa.system_memory_pool()
                os.environ['ARROW_DEFAULT_MEMORY_POOL'] = 'system'
        except Exception:
            pass

    db.init()
    mp.freeze_support()

    environment = config["environment"]
    if environment == "aws_marketplace":
        try:
            register_oauth_client()
        except Exception as e:
            logger.error(f"Something went wrong during client register: {e}")
    elif environment != "local":
        try:
            aws_meta_data = get_aws_meta_data()
            config.update({
                'aws_meta_data': aws_meta_data
            })
        except Exception:
            pass

    is_cloud = config.is_cloud

    if not is_cloud:
        logger.debug("Applying database migrations")
        try:
            from mindsdb.migrations import migrate
            migrate.migrate_to_head()
        except Exception as e:
            logger.error(f"Error! Something went wrong during DB migrations: {e}")

        logger.debug(f"Checking if default project {config.get('default_project')} exists")
        project_controller = ProjectController()

        current_default_project = project_controller.get(is_default=True)
        if current_default_project.record.name != config.get('default_project'):
            try:
                new_default_project = project_controller.get(name=config.get('default_project'))
                log.critical(f"A project with the name '{config.get('default_project')}' already exists")
                sys.exit(1)
            except EntityNotExistsError:
                pass
            project_controller.update(current_default_project.record.id, new_name=config.get('default_project'))

    apis = os.getenv('MINDSDB_APIS') or config.cmd_args.api

    if apis is None:  # If "--api" option is not specified, start the default APIs
        api_arr = [TrunkProcessEnum.HTTP, TrunkProcessEnum.MYSQL]
    elif apis == "":  # If "--api=" (blank) is specified, don't start any APIs
        api_arr = []
    else:  # The user has provided a list of APIs to start
        api_arr = [TrunkProcessEnum(name) for name in apis.split(',')]

    if config.cmd_args.install_handlers is not None:
        handlers_list = [s.strip() for s in config.cmd_args.install_handlers.split(",")]
        # import_meta = handler_meta.get('import', {})
        for handler_name, handler_meta in integration_controller.get_handlers_import_status().items():
            if handler_name not in handlers_list:
                continue
            import_meta = handler_meta.get("import", {})
            if import_meta.get("success") is True:
                logger.info(f"{'{0: <18}'.format(handler_name)} - already installed")
                continue
            result = install_dependencies(import_meta.get("dependencies", []))
            if result.get("success") is True:
                logger.info(
                    f"{'{0: <18}'.format(handler_name)} - successfully installed"
                )
            else:
                logger.info(
                    f"{'{0: <18}'.format(handler_name)} - error during dependencies installation: {result.get('error_message', 'unknown error')}"
                )
        sys.exit(0)

    logger.info(f"Version: {mindsdb_version}")
    logger.info(f"Configuration file: {config.config_path or 'absent'}")
    logger.info(f"Storage path: {config.paths['root']}")
    logger.debug(f"User config: {config.user_config}")
    logger.debug(f"System config: {config.auto_config}")
    logger.debug(f"Env config: {config.env_config}")

    unexisting_pids = clean_unlinked_process_marks()
    if not is_cloud:
        if len(unexisting_pids) > 0:
            set_error_model_status_by_pids(unexisting_pids)
        set_error_model_status_for_unfinished()

        integration_controller.create_permanent_integrations()

        # region Mark old predictors as outdated
        is_modified = False
        predictor_records = (
            db.session.query(db.Predictor)
            .filter(db.Predictor.deleted_at.is_(None))
            .all()
        )
        if len(predictor_records) > 0:
            (
                sucess,
                compatible_versions,
            ) = get_versions_where_predictors_become_obsolete()
            if sucess is True:
                compatible_versions = [version.parse(x) for x in compatible_versions]
                mindsdb_version_parsed = version.parse(mindsdb_version)
                compatible_versions = [x for x in compatible_versions if x <= mindsdb_version_parsed]
                if len(compatible_versions) > 0:
                    last_compatible_version = compatible_versions[-1]
                    for predictor_record in predictor_records:
                        if (
                            isinstance(predictor_record.mindsdb_version, str)
                            and version.parse(predictor_record.mindsdb_version) < last_compatible_version
                        ):
                            predictor_record.update_status = "available"
                            is_modified = True
        if is_modified is True:
            db.session.commit()
        # endregion

    clean_process_marks()

    http_api_config = config['api']['http']
    mysql_api_config = config['api']['mysql']
    mcp_api_config = config['api']['mcp']
    trunc_processes_struct = {
        TrunkProcessEnum.HTTP: TrunkProcessData(
            name=TrunkProcessEnum.HTTP.value,
            entrypoint=start_http,
            port=http_api_config['port'],
            args=(config.cmd_args.verbose, config.cmd_args.no_studio),
            restart_on_failure=http_api_config.get('restart_on_failure', False),
            max_restart_count=http_api_config.get('max_restart_count', TrunkProcessData.max_restart_count),
            max_restart_interval_seconds=http_api_config.get(
                'max_restart_interval_seconds', TrunkProcessData.max_restart_interval_seconds
            )
        ),
        TrunkProcessEnum.MYSQL: TrunkProcessData(
            name=TrunkProcessEnum.MYSQL.value,
            entrypoint=start_mysql,
            port=mysql_api_config['port'],
            args=(config.cmd_args.verbose,),
            restart_on_failure=mysql_api_config.get('restart_on_failure', False),
            max_restart_count=mysql_api_config.get('max_restart_count', TrunkProcessData.max_restart_count),
            max_restart_interval_seconds=mysql_api_config.get(
                'max_restart_interval_seconds', TrunkProcessData.max_restart_interval_seconds
            )
        ),
        TrunkProcessEnum.MONGODB: TrunkProcessData(
            name=TrunkProcessEnum.MONGODB.value,
            entrypoint=start_mongo,
            port=config['api']['mongodb']['port'],
            args=(config.cmd_args.verbose,)
        ),
        TrunkProcessEnum.POSTGRES: TrunkProcessData(
            name=TrunkProcessEnum.POSTGRES.value,
            entrypoint=start_postgres,
            port=config['api']['postgres']['port'],
            args=(config.cmd_args.verbose,)
        ),
        TrunkProcessEnum.JOBS: TrunkProcessData(
            name=TrunkProcessEnum.JOBS.value,
            entrypoint=start_scheduler,
            args=(config.cmd_args.verbose,)
        ),
        TrunkProcessEnum.TASKS: TrunkProcessData(
            name=TrunkProcessEnum.TASKS.value,
            entrypoint=start_tasks,
            args=(config.cmd_args.verbose,)
        ),
        TrunkProcessEnum.ML_TASK_QUEUE: TrunkProcessData(
            name=TrunkProcessEnum.ML_TASK_QUEUE.value,
            entrypoint=start_ml_task_queue,
            args=(config.cmd_args.verbose,)
        ),
        TrunkProcessEnum.MCP: TrunkProcessData(
            name=TrunkProcessEnum.MCP.value,
            entrypoint=start_mcp,
            port=mcp_api_config.get('port', 47337),
            args=(config.cmd_args.verbose,),
            restart_on_failure=mcp_api_config.get('restart_on_failure', False),
            max_restart_count=mcp_api_config.get('max_restart_count', TrunkProcessData.max_restart_count),
            max_restart_interval_seconds=mcp_api_config.get(
                'max_restart_interval_seconds', TrunkProcessData.max_restart_interval_seconds
            )
        )
    }

    for api_enum in api_arr:
        if api_enum in trunc_processes_struct:
            trunc_processes_struct[api_enum].need_to_run = True
        else:
            logger.error(f"ERROR: {api_enum} API is not a valid api in config")

    if config['jobs']['disable'] is False:
        trunc_processes_struct[TrunkProcessEnum.JOBS].need_to_run = True

    if config['tasks']['disable'] is False:
        trunc_processes_struct[TrunkProcessEnum.TASKS].need_to_run = True

    if config.cmd_args.ml_task_queue_consumer is True:
        trunc_processes_struct[TrunkProcessEnum.ML_TASK_QUEUE].need_to_run = True

    def start_process(trunc_process_data):
        # TODO this 'ctx' is eclipsing 'context' class imported as 'ctx'
        ctx = mp.get_context("spawn")
        logger.info(f"{trunc_process_data.name} API: starting...")
        try:
            trunc_process_data.process = ctx.Process(
                target=trunc_process_data.entrypoint,
                args=trunc_process_data.args,
                name=trunc_process_data.name
            )
            trunc_process_data.process.start()
        except Exception as e:
            logger.error(
                f"Failed to start {trunc_process_data.name} API with exception {e}\n{traceback.format_exc()}"
            )
            close_api_gracefully(trunc_processes_struct)
            raise e

    for trunc_process_data in trunc_processes_struct.values():
        if trunc_process_data.started is True or trunc_process_data.need_to_run is False:
            continue
        start_process(trunc_process_data)

    atexit.register(close_api_gracefully, trunc_processes_struct=trunc_processes_struct)

    async def wait_api_start(api_name, pid, port):
        timeout = 60
        start_time = time.time()
        started = is_pid_listen_port(pid, port)
        while (time.time() - start_time) < timeout and started is False:
            await asyncio.sleep(0.5)
            started = is_pid_listen_port(pid, port)
        return api_name, port, started

    async def wait_apis_start():
        futures = [
            wait_api_start(
                trunc_process_data.name,
                trunc_process_data.process.pid,
                trunc_process_data.port
            )
            for trunc_process_data in trunc_processes_struct.values()
            if trunc_process_data.port is not None and trunc_process_data.need_to_run is True
        ]
        for future in asyncio.as_completed(futures):
            api_name, port, started = await future
            if started:
                logger.info(f"{api_name} API: started on {port}")
            else:
                logger.error(f"ERROR: {api_name} API cant start on {port}")

    async def join_process(trunc_process_data: TrunkProcessData):
        finish = False
        while not finish:
            process = trunc_process_data.process
            try:
                while process.is_alive():
                    process.join(1)
                    await asyncio.sleep(0)
            except KeyboardInterrupt:
                logger.info("Got keyboard interrupt, stopping APIs")
                close_api_gracefully(trunc_processes_struct)
            finally:
                if trunc_process_data.should_restart:
                    if trunc_process_data.request_restart_attempt():
                        logger.warning(f"{trunc_process_data.name} API: stopped unexpectedly, restarting")
                        trunc_process_data.process = None
                        if trunc_process_data.name == TrunkProcessEnum.HTTP.value:
                            # do not open GUI on HTTP API restart
                            trunc_process_data.args = (config.cmd_args.verbose, True)
                        start_process(trunc_process_data)
                        api_name, port, started = await wait_api_start(
                            trunc_process_data.name,
                            trunc_process_data.process.pid,
                            trunc_process_data.port
                        )
                        if started:
                            logger.info(f"{api_name} API: started on {port}")
                        else:
                            logger.error(f"ERROR: {api_name} API cant start on {port}")
                    else:
                        finish = True
                        logger.error(
                            f'The "{trunc_process_data.name}" process could not restart after failure. '
                            'There will be no further attempts to restart.'
                        )
                else:
                    finish = True
                    logger.info(f"{trunc_process_data.name} API: stopped")

    async def gather_apis():
        await asyncio.gather(
            *[
                join_process(trunc_process_data)
                for trunc_process_data in trunc_processes_struct.values()
                if trunc_process_data.need_to_run is True
            ],
            return_exceptions=False
        )

    ioloop = asyncio.new_event_loop()
    ioloop.run_until_complete(wait_apis_start())

    threading.Thread(target=do_clean_process_marks, name='clean_process_marks').start()

    ioloop.run_until_complete(gather_apis())
    ioloop.close()
