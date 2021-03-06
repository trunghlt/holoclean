from functools import partial
import logging
from multiprocessing import Pool
from string import Template
import time
import hashlib

import psycopg2
import sqlalchemy as sql
from sqlalchemy.schema import CreateSchema, DropSchema

index_template = Template('CREATE INDEX $idx_title ON "$table" ($attrs)')
drop_table_template = Template('DROP TABLE IF EXISTS "$table"')
create_table_template = Template('CREATE TABLE "$table" AS ($stmt)')

class DBengine:
    """
    A wrapper class for postgresql engine.
    Maintains connections and executes queries.
    """
    def __init__(self, sqlalchemy_uri, pool_size=20, timeout=60000):
        self.timeout = timeout
        self._pool = Pool(pool_size) if pool_size > 1 else None
        self.conn = sqlalchemy_uri
        self.dbschema = self.generate_hash()
        try:
            self.engine = sql.create_engine(self.conn, client_encoding='utf8', pool_size=pool_size,
                                            connect_args={'options': '-csearch_path={}'.format(self.dbschema)})
        except TypeError:
            self.engine = sql.create_engine(self.conn, connect_args={'options': '-csearch_path={}'.format(self.dbschema)})
        finally:
            logging.debug(f'Creating schema: {self.dbschema}')
            self.engine.execute(CreateSchema(self.dbschema))

    @staticmethod
    def generate_hash():
        h = hashlib.sha1()
        h.update(str(time.time()).encode('utf-8'))
        return f"temp_{h.hexdigest()[:10]}"

    def execute_queries(self, queries):
        """
        Executes :param queries: in parallel.

        :param queries: (list[str]) list of SQL queries to be executed
        """
        logging.debug('Preparing to execute %d queries.', len(queries))
        tic = time.clock()
        results = self._apply_func(partial(_execute_query, conn_uri=self.conn, conn_args={'options': '-csearch_path={}'.format(self.dbschema)}), [(idx, q) for idx, q in enumerate(queries)])
        toc = time.clock()
        logging.debug('Time to execute %d queries: %.2f secs', len(queries), toc-tic)
        return results

    def execute_queries_w_backup(self, queries):
        """
        Executes :param queries: that have backups in parallel. Used in featurization.

        :param queries: (list[str]) list of SQL queries to be executed
        """
        logging.debug('Preparing to execute %d queries.', len(queries))
        tic = time.clock()
        results = self._apply_func(
            partial(_execute_query_w_backup, conn_uri=self.conn, conn_args={'options': '-csearch_path={}'.format(self.dbschema)}, timeout=self.timeout),
            [(idx, q) for idx, q in enumerate(queries)])
        toc = time.clock()
        logging.debug('Time to execute %d queries: %.2f secs', len(queries), toc-tic)
        return results

    def execute_query(self, query):
        """
        Executes a single :param query: using current connection.

        :param query: (str) SQL query to be executed
        """
        tic = time.clock()
        conn = self.engine.connect()
        result = conn.execute(query).fetchall()
        conn.close()
        toc = time.clock()
        logging.debug('Time to execute query: %.2f secs', toc-tic)
        return result

    def create_db_table_from_query(self, name, query):
        tic = time.clock()
        drop = drop_table_template.substitute(table=name)
        create = create_table_template.substitute(table=name, stmt=query)
        conn = self.engine.connect()
        conn.execute(drop)
        conn.execute(create)
        conn.close()
        toc = time.clock()
        logging.debug('Time to create table: %.2f secs', toc-tic)
        return True

    def create_db_index(self, name, table, attr_list):
        """
        create_db_index creates a (multi-column) index on the columns/attributes
        specified in :param attr_list: with the given :param name: on
        :param table:.

        :param name: (str) name of index
        :param table: (str) name of table
        :param attr_list: (list[str]) list of attributes/columns to create index on
        """
        # We need to quote each attribute since Postgres auto-downcases unquoted column references
        quoted_attrs = map(lambda attr: '"{}"'.format(attr), attr_list)
        stmt = index_template.substitute(idx_title=name, table=table, attrs=','.join(quoted_attrs))
        tic = time.clock()
        conn = self.engine.connect()
        result = conn.execute(stmt)
        conn.close()
        toc = time.clock()
        logging.debug('Time to create index: %.2f secs', toc-tic)
        return result

    def _apply_func(self, func, collection):
        if self._pool is None:
            return list(map(func, collection))
        return self._pool.map(func, collection)

    def clean_up(self):
        logging.debug(f'Dropping schema: {self.dbschema}')
        self.engine.execute(DropSchema(self.dbschema, cascade=True))


def _execute_query(args, conn_uri, conn_args):
    query_id = args[0]
    query = args[1]
    logging.debug("Starting to execute query %s with id %s", query, query_id)
    tic = time.clock()
    engine = sql.create_engine(conn_uri, connect_args=conn_args)
    with engine.connect() as conn:
        res = conn.execute(query).fetchall()
    toc = time.clock()
    logging.debug('Time to execute query with id %d: %.2f secs', query_id, (toc - tic))
    return res


def _execute_query_w_backup(args, conn_uri, conn_args, timeout):
    query_id = args[0]
    query = args[1][0]
    query_backup = args[1][1]
    logging.debug("Starting to execute query %s with id %s", query, query_id)
    tic = time.clock()
    engine = sql.create_engine(conn_uri, connect_args=conn_args)
    with engine.connect() as conn:
        conn.execute("SET statement_timeout to %d;" % timeout)
        try:
            res = conn.execute(query).fetchall()
        except psycopg2.extensions.QueryCanceledError as e:
            logging.debug("Failed to execute query %s with id %s. Timeout reached.", query, query_id)

            # No backup query, simply return empty result
            if not query_backup:
                logging.warn("no backup query to execute, returning empty query results")
                return []

            logging.debug("Starting to execute backup query %s with id %s", query_backup, query_id)
            res = conn.execute(query_backup).fetchall()
            toc = time.clock()
            logging.debug('Time to execute query with id %d: %.2f secs', query_id, toc - tic)
    return res
