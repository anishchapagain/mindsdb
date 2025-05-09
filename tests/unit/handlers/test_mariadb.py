from collections import OrderedDict
import unittest
from unittest.mock import patch

from mysql.connector import Error as MySQLError

from base_handler_test import BaseDatabaseHandlerTest
from mindsdb.integrations.handlers.mariadb_handler.mariadb_handler import MariaDBHandler


class TestMariaDBHandler(BaseDatabaseHandlerTest, unittest.TestCase):

    @property
    def dummy_connection_data(self):
        return OrderedDict(
            host='127.0.0.1',
            port=3307,
            user='example_user',
            password='example_pass',
            database='example_db',
        )

    @property
    def err_to_raise_on_connect_failure(self):
        return MySQLError("Connection Failed")

    @property
    def get_tables_query(self):
        return """
            SELECT
                TABLE_SCHEMA AS table_schema,
                TABLE_NAME AS table_name,
                TABLE_TYPE AS table_type
            FROM
                information_schema.TABLES
            WHERE
                TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                AND TABLE_SCHEMA = DATABASE()
            ORDER BY 2
            ;
        """

    @property
    def get_columns_query(self):
        return f"""
            select
                COLUMN_NAME,
                DATA_TYPE,
                ORDINAL_POSITION,
                COLUMN_DEFAULT,
                IS_NULLABLE,
                CHARACTER_MAXIMUM_LENGTH,
                CHARACTER_OCTET_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE,
                DATETIME_PRECISION,
                CHARACTER_SET_NAME,
                COLLATION_NAME
            from
                information_schema.columns
            where
                table_name = '{self.mock_table}';
        """

    def create_handler(self):
        return MariaDBHandler('mariadb', connection_data=self.dummy_connection_data)

    def create_patcher(self):
        return patch('mysql.connector.connect')


if __name__ == '__main__':
    unittest.main()
