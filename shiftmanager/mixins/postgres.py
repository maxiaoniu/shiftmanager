#!/usr/bin/env python

"""
Mixin classes for working with Postgres database exports
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from datetime import datetime
import json
import tempfile

import psycopg2

from shiftmanager.memoized_property import memoized_property
from shiftmanager.mixins.s3 import S3Mixin
from shiftmanager import util


class PostgresMixin(S3Mixin):
    """The Postgres interaction base class for `Redshift`."""

    @memoized_property
    def pg_connection(self):
        """A `psycopg2.connect` connection to Postgres.

        Instantiation is delayed until the object is first used.
        """
        print("Connecting to %s..." % self.pg_host)
        return psycopg2.connect(user=self.pg_user,
                                host=self.pg_host,
                                port=self.pg_port,
                                database=self.pg_database,
                                password=self.pg_password)

    def pg_execute_and_commit_single_statement(self, statement):
        """Execute single Postgres statement"""
        with self.pg_connection as conn:
            with conn.cursor() as cur:
                cur.execute(statement)

    def create_pg_connection(self, database=None, user=None, password=None,
                             host='localhost', port=5432):
        """
        Create a `psycopg2.connect` connection to Redshift.
        """

        self.pg_user = user
        self.pg_host = host
        self.pg_port = port
        self.pg_database = database
        self.pg_password = password
        return self.pg_connection

    def pg_copy_table_to_csv(self, csv_file_path, pg_table_name=None,
                             pg_select_statement=None):
        """
        Use Postgres to COPY the given table_name to a csv file at the given
        csv_path.

        Additionally, fetch the row count of the given table_name for
        further processing.

        Parameters
        ----------
        csv_file_path: str
            File path for the CSV to be written to by Postgres
        pg_table_name: str
            Optional Postgres table name to be written to CSV if user
            does not want to specify subset
        pg_select_statement: str
            Optional select statement if user wants to specify subset of table


        Returns
        -------
        row_count: int
        """
        copy = "COPY {pg_table_or_select} TO '{csv_fp}' DELIMITER ',' CSV;"

        if pg_select_statement is None and pg_table_name is not None:

            formatted_statement = copy.format(
                pg_table_or_select=pg_table_name,
                csv_fp=csv_file_path)

        elif pg_select_statement is not None and pg_table_name is None:

            if not (pg_select_statement.startswith("(") and
                    pg_select_statement.endswith(")")):
                pg_select_statement = "(" + pg_select_statement + ")"

            formatted_statement = copy.format(
                pg_table_or_select=pg_select_statement,
                csv_fp=csv_file_path)

        else:
            ValueError("Please enter a table name or a select statement.")

        with self.pg_connection as conn:
            with conn.cursor() as cur:
                cur.execute(formatted_statement)
                row_count = cur.rowcount

        return row_count

    def get_csv_chunk_generator(self, csv_file_path, row_count, chunks):
        """
        Given the csv_file_path and a row_count, yield chunks number
        of string chunks

        Parameters
        ----------
        csv_file_path: str
            File path for the CSV written by Postgres
        row_count: int
            Number of rows in the CSV
        chunks: int
            Number of chunks to yield

        Yields
        ------
        str
        """

        # Get chunk boundaries
        left_closed_boundary = util.linspace(0, row_count, chunks)
        left_closed_boundary.append(row_count - 1)
        right_closed_boundary = left_closed_boundary[1:]
        final_boundary_index = len(right_closed_boundary) - 1

        # We're going to allocate a large buffer for this -- let's read as fast
        # as possible
        chunk_lines = []
        boundary_index = 0
        boundary = right_closed_boundary[boundary_index]
        one_mebibyte = 1048576
        with open(csv_file_path, "r", one_mebibyte) as f:
            for line_number, row in enumerate(f):
                chunk_lines.append(row)
                if line_number == boundary:
                    if boundary_index != final_boundary_index:
                        boundary_index += 1
                        boundary = right_closed_boundary[boundary_index]
                    yield "".join(chunk_lines)
                    chunk_lines = []

    def _create_copy_statement(self, table_name, manifest_key_path):
        """Create Redshift copy statement for given table_name and
        the provided manifest_key_path.

        Parameters
        ----------
        table_name: str
            Redshift table name to COPY to
        manifest_key_path: str
            Complete S3 path to .manifest file

        Returns
        -------
        str
        """

        key_id = 'aws_access_key_id={};'.format(self.aws_access_key_id)
        secret_key_id = 'aws_secret_access_key={}'.format(
            self.aws_secret_access_key)

        return """copy {table_name}
                  from '{manifest_key_path}'
                  credentials '{key_id}{secret_key_id}'
                  manifest
                  csv;""".format(table_name=table_name,
                                 manifest_key_path=manifest_key_path,
                                 key_id=key_id,
                                 secret_key_id=secret_key_id)

    def copy_table_to_redshift(self, redshift_table_name,
                               bucket_name, key_prefix, slices,
                               pg_table_name=None, pg_select_statement=None):
        """
        Write the contents of a Postgres table to Redshift.
        Write the table to the given bucket under the given
        key prefix.

        Parameters
        ----------
        redshift_table_name: str
            Redshift table to which CSVs are to be written
        bucket_name: str
            The name of the S3 bucket to be written to
        key_prefix: str
            The key path within the bucket to write to
        slices: int
            The number of slices in user's Redshift cluster, used to
            split CSV into chunks for parallel data loading
        pg_table_name: str
            Optional Postgres table name to be written to CSV if user
            does not want to specify subset
        pg_select_statement: str
            Optional select statement if user wants to specify subset of table
        """
        if not self.table_exists(redshift_table_name):
            raise ValueError("This table_name does not exist in Redshift!")

        bucket = self.get_bucket(bucket_name)
        # All keys written to S3 in the event cleanup is needed
        all_s3_keys = []

        if not key_prefix.endswith("/"):
            final_key_prefix = "".join([key_prefix, "/"])
        else:
            final_key_prefix = key_prefix

        with tempfile.NamedTemporaryFile() as ntf:
            csv_temp_path = ntf.name
            row_count = self.pg_copy_table_to_csv(
                csv_temp_path, pg_table_name=pg_table_name,
                pg_select_statement=pg_select_statement)
            chunk_generator = self.get_csv_chunk_generator(csv_temp_path,
                                                           row_count, slices)
            backfill_timestamp = datetime.utcnow().strftime(
                "%Y-%m-%d_%H-%M-%S")

            manifest_entries = []
            for count, chunk in enumerate(chunk_generator):
                chunk_name = "_".join([backfill_timestamp, "chunk",
                                       str(count)])
                complete_key_path = "".join([final_key_prefix,
                                             chunk_name, '.csv'])

                print('Writing {} to S3...'.format(complete_key_path))
                self.write_string_to_s3(chunk, bucket, complete_key_path)
                all_s3_keys.append(complete_key_path)

                s3_path = (complete_key_path
                           if complete_key_path.startswith("/")
                           else "".join(["/", complete_key_path]))
                manifest_entries.append({
                    'url': "".join(['s3://', bucket.name, s3_path]),
                    'mandatory': True
                })

            manifest = {'entries': manifest_entries}
            manifest_key_path = "".join([final_key_prefix,
                                         backfill_timestamp, ".manifest"])
            manifest_key = bucket.new_key(manifest_key_path)
            all_s3_keys.append(manifest_key_path)

            print('Writing .manifest file to S3...')
            manifest_key.set_contents_from_string(json.dumps(manifest),
                                                  encrypt_key=True)
            complete_manifest_path = "".join(['s3://', bucket.name,
                                              manifest_key_path])
            copy_statement = self._create_copy_statement(
                redshift_table_name, complete_manifest_path)

            print('Copying from S3 to Redshift...')
            try:
                self.execute(copy_statement)
            except:
                # Clean up S3 bucket in the event of any exception
                print("Error writing to Redshift! Cleaning up S3...")
                for key in all_s3_keys:
                    bucket.delete_key(key)
                raise
