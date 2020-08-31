from abc import ABCMeta, abstractmethod
from collections import namedtuple
from contextlib import contextmanager
import csv
from enum import Enum
import io
import itertools
import os
import pathlib
import tempfile
import time
from typing import Dict, Any, List

import lxml.etree as ET
import requests

from cumulusci.core.exceptions import BulkDataException
from cumulusci.core.utils import process_bool_arg


def get_batch_iterator(iterator, n):
    while True:
        batch = list(itertools.islice(iterator, n))
        if not batch:
            return

        yield batch


class DataOperationType(Enum):
    """Enum defining the API data operation requested."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    HARD_DELETE = "hardDelete"
    QUERY = "query"


class DataApi(Enum):
    """Enum defining requested Salesforce data API for an operation."""

    BULK = "bulk"
    REST = "rest"
    SMART = "smart"


class DataOperationStatus(Enum):
    """Enum defining outcome values for a data operation."""

    SUCCESS = "Success"
    ROW_FAILURE = "Row failure"
    JOB_FAILURE = "Job failure"
    IN_PROGRESS = "In progress"
    ABORTED = "Aborted"


DataOperationResult = namedtuple("Result", ["id", "success", "error"])
DataOperationJobResult = namedtuple(
    "DataOperationJobResult",
    ["status", "job_errors", "records_processed", "total_row_errors"],
)


@contextmanager
def download_file(uri, bulk_api):
    """Download the Bulk API result file for a single batch,
    and remove it when the context manager exits."""
    try:
        (handle, path) = tempfile.mkstemp(text=False)
        resp = requests.get(uri, headers=bulk_api.headers(), stream=True)
        resp.raise_for_status()
        f = os.fdopen(handle, "wb")
        for chunk in resp.iter_content(chunk_size=None):
            f.write(chunk)

        f.close()
        with open(path, "r", newline="", encoding="utf-8") as f:
            yield f
    finally:
        pathlib.Path(path).unlink()


class BulkJobMixin:
    """Provides mixin utilities for classes that manage Bulk API jobs."""

    def _job_state_from_batches(self, job_id):
        """Query for batches under job_id and return overall status
        inferred from batch-level status values."""
        uri = f"{self.bulk.endpoint}/job/{job_id}/batch"
        response = requests.get(uri, headers=self.bulk.headers())
        response.raise_for_status()
        return self._parse_job_state(response.content)

    def _parse_job_state(self, xml):
        """Parse the Bulk API return value and generate a summary status record for the job."""
        tree = ET.fromstring(xml)
        statuses = [el.text for el in tree.iterfind(".//{%s}state" % self.bulk.jobNS)]
        state_messages = [
            el.text for el in tree.iterfind(".//{%s}stateMessage" % self.bulk.jobNS)
        ]

        failures = tree.find(".//{%s}numberRecordsFailed" % self.bulk.jobNS)
        record_failure_count = int(failures.text) if failures is not None else 0
        processed = tree.find(".//{%s}numberRecordsProcessed" % self.bulk.jobNS)
        records_processed = int(processed.text) if processed is not None else 0

        # FIXME: "Not Processed" to be expected for original batch with PK Chunking Query
        # PK Chunking is not currently supported.
        if "Not Processed" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.ABORTED, [], records_processed, record_failure_count
            )
        elif "InProgress" in statuses or "Queued" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.IN_PROGRESS,
                [],
                records_processed,
                record_failure_count,
            )
        elif "Failed" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.JOB_FAILURE,
                state_messages,
                records_processed,
                record_failure_count,
            )

        if record_failure_count:
            return DataOperationJobResult(
                DataOperationStatus.ROW_FAILURE,
                [],
                records_processed,
                record_failure_count,
            )

        return DataOperationJobResult(
            DataOperationStatus.SUCCESS, [], records_processed, record_failure_count
        )

    def _wait_for_job(self, job_id):
        """Wait for the given job to enter a completed state (success or failure)."""
        while True:
            job_status = self.bulk.job_status(job_id)
            self.logger.info(
                f"Waiting for job {job_id} ({job_status['numberBatchesCompleted']}/{job_status['numberBatchesTotal']} batches complete)"
            )
            result = self._job_state_from_batches(job_id)
            if result.status is not DataOperationStatus.IN_PROGRESS:
                break

            time.sleep(10)
        self.logger.info(f"Job {job_id} finished with result: {result.status.value}")
        if result.status is DataOperationStatus.JOB_FAILURE:
            for state_message in result.job_errors:
                self.logger.error(f"Batch failure message: {state_message}")

        return result


class BaseDataOperation(metaclass=ABCMeta):
    """Abstract base class for all data operations (queries and DML)."""

    def __init__(self, *, sobject, operation, api_options, context):
        self.sobject = sobject
        self.operation = operation
        self.api_options = api_options
        self.context = context
        self.bulk = context.bulk
        self.sf = context.sf
        self.logger = context.logger
        self.job_result = None


class BaseQueryOperation(BaseDataOperation, metaclass=ABCMeta):
    """Abstract base class for query operations in all APIs."""

    def __init__(self, *, sobject, api_options, context, query):
        super().__init__(
            sobject=sobject,
            operation=DataOperationType.QUERY,
            api_options=api_options,
            context=context,
        )
        self.soql = query

    def __enter__(self):
        self.query()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    @abstractmethod
    def query(self):
        """Execute requested query and block until results are available."""
        pass

    @abstractmethod
    def get_results(self):
        """Return a generator of rows from the query."""
        pass


class BulkApiQueryOperation(BaseQueryOperation, BulkJobMixin):
    """Operation class for Bulk API query jobs."""

    def query(self):
        self.job_id = self.bulk.create_query_job(self.sobject, contentType="CSV")
        self.logger.info(f"Created Bulk API query job {self.job_id}")
        self.batch_id = self.bulk.query(self.job_id, self.soql)

        self.job_result = self._wait_for_job(self.job_id)
        self.bulk.close_job(self.job_id)

    def get_results(self):
        # FIXME: For PK Chunking, need to get new batch Ids
        # and retrieve their results. Original batch will not be processed.

        result_ids = self.bulk.get_query_batch_result_ids(
            self.batch_id, job_id=self.job_id
        )
        for result_id in result_ids:
            uri = f"{self.bulk.endpoint}/job/{self.job_id}/batch/{self.batch_id}/result/{result_id}"

            with download_file(uri, self.bulk) as f:
                reader = csv.reader(f)
                self.headers = next(reader)
                if "Records not found for this query" in self.headers:
                    return

                yield from reader


class RestApiQueryOperation(BaseQueryOperation):
    """Operation class for REST API query jobs."""

    def __init__(self, *, sobject, fields, api_options, context, query):
        super().__init__(
            sobject=sobject, api_options=api_options, context=context, query=query
        )
        self.fields = fields

    def query(self):
        # This is not as memory-efficient as we might like.
        # To preserve the invariant from the Bulk API that
        # we know the query count before consuming the results,
        # we use `query_all` instead of `query_all_iter`.
        self.response = self.sf.query_all(self.soql)
        self.job_result = DataOperationJobResult(
            DataOperationStatus.SUCCESS, [], self.response["totalSize"], 0
        )

    def get_results(self):
        def convert(rec):
            return [str(rec[f]) if rec[f] is not None else None for f in self.fields]

        yield from (convert(rec) for rec in self.response["records"])


class BaseDmlOperation(BaseDataOperation, metaclass=ABCMeta):
    """Abstract base class for DML operations in all APIs."""

    def __init__(self, *, sobject, operation, api_options, context, fields):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
        )
        self.fields = fields

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.end()

    def start(self):
        """Perform any required setup, such as job initialization, for the operation."""
        pass

    @abstractmethod
    def load_records(self, records):
        """Perform the requested DML operation on the supplied row iterator."""
        pass

    def end(self):
        """Perform any required teardown for the operation before results are returned."""
        pass

    @abstractmethod
    def get_results(self):
        """Return a generator of DataOperationResult objects."""
        pass


class BulkApiDmlOperation(BaseDmlOperation, BulkJobMixin):
    """Operation class for all DML operations run using the Bulk API."""

    def __init__(self, *, sobject, operation, api_options, context, fields):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
            fields=fields,
        )
        self.csv_buff = io.StringIO(newline="")
        self.csv_writer = csv.writer(self.csv_buff)

    def start(self):
        self.job_id = self.bulk.create_job(
            self.sobject,
            self.operation.value,
            contentType="CSV",
            concurrency=self.api_options.get("bulk_mode", "Parallel"),
        )

    def end(self):
        self.bulk.close_job(self.job_id)
        self.job_result = self._wait_for_job(self.job_id)

    def load_records(self, records):
        self.batch_ids = []

        for count, csv_batch in enumerate(self._batch(records)):
            self.context.logger.info(f"Uploading batch {count + 1}")
            self.batch_ids.append(self.bulk.post_batch(self.job_id, iter(csv_batch)))

    def _batch(self, records, n=10000, char_limit=10000000):
        """Given an iterator of records, yields batches of
        records serialized in .csv format.

        Batches adhere to the following, in order of precedence:
        (1) They do not exceed the given character limit
        (2) They do not contain more than n records per batch
        """
        serialized_csv_fields = self._serialize_csv_record(self.fields)
        len_csv_fields = len(serialized_csv_fields)

        # append fields to first row
        batch = [serialized_csv_fields]
        current_chars = len_csv_fields
        for record in records:
            serialized_record = self._serialize_csv_record(record)
            # Does the next record put us over the character limit?
            if len(serialized_record) + current_chars > char_limit:
                yield batch
                batch = [serialized_csv_fields]
                current_chars = len_csv_fields

            batch.append(serialized_record)
            current_chars += len(serialized_record)

            # yield batch if we're at desired size
            # -1 due to first row being field names
            if len(batch) - 1 == n:
                yield batch
                batch = [serialized_csv_fields]
                current_chars = len_csv_fields

        # give back anything leftover
        if len(batch) > 1:
            yield batch

    def _serialize_csv_record(self, record):
        """Given a list of strings (record) return
        the corresponding record serialized in .csv format"""
        self.csv_writer.writerow(record)
        serialized = self.csv_buff.getvalue().encode("utf-8")
        # flush buffer
        self.csv_buff.truncate(0)
        self.csv_buff.seek(0)

        return serialized

    def get_results(self):
        for batch_id in self.batch_ids:
            try:
                results_url = (
                    f"{self.bulk.endpoint}/job/{self.job_id}/batch/{batch_id}/result"
                )
                # Download entire result file to a temporary file first
                # to avoid the server dropping connections
                with download_file(results_url, self.bulk) as f:
                    self.logger.info(f"Downloaded results for batch {batch_id}")

                    reader = csv.reader(f)
                    next(reader)  # skip header

                    for row in reader:
                        success = process_bool_arg(row[1])
                        yield DataOperationResult(
                            row[0] if success else None,
                            success,
                            row[3] if not success else None,
                        )
            except Exception as e:
                raise BulkDataException(
                    f"Failed to download results for batch {batch_id} ({str(e)})"
                )


class RestApiDmlOperation(BaseDmlOperation):
    def __init__(self, *, sobject, operation, api_options, context, fields):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
            fields=fields,
        )

        # Because we send values in JSON, we must convert Booleans and nulls
        describe = {
            field["name"]: field
            for field in getattr(context.sf, sobject).describe()["fields"]
        }
        self.boolean_fields = [f for f in fields if describe[f]["type"] == "boolean"]

    def load_records(self, records):
        def _convert(rec):
            r = dict(zip(self.fields, rec))
            for boolean_field in self.boolean_fields:
                r[boolean_field] = bool(r[boolean_field])

            # Remove empty fields (different semantics in REST API)
            # We do this for insert only - on update, any fields set to `null`
            # are meant to be blanked out.
            if self.operation is DataOperationType.INSERT:
                r = {k: r[k] for k in r if r[k] is not None and r[k] != ""}

            print(r)
            r["attributes"] = {"type": self.sobject}
            return r

        self.results = []
        method = {
            DataOperationType.INSERT: "POST",
            DataOperationType.UPDATE: "PATCH",
            DataOperationType.DELETE: "DELETE",
        }[self.operation]

        for chunk in get_batch_iterator(
            records, self.api_options.get("batch_size", 200)
        ):
            if self.operation is DataOperationType.DELETE:
                url_string = "?ids=" + ",".join(_convert(rec)["Id"] for rec in chunk)
                json = None
            else:
                url_string = ""
                json = {"allOrNone": False, "records": [_convert(rec) for rec in chunk]}

            self.results.extend(
                self.sf.restful(
                    f"composite/sobjects{url_string}", method=method, json=json
                )
            )

        row_errors = len([res for res in self.results if not res["success"]])
        self.job_result = DataOperationJobResult(
            DataOperationStatus.SUCCESS
            if not row_errors
            else DataOperationStatus.ROW_FAILURE,
            [],
            len(self.results),
            row_errors,
        )

    def get_results(self):
        """Return a generator of DataOperationResult objects."""

        def _convert(res):
            # TODO: make DataOperationResult handle this error variant
            if res.get("errors"):
                errors = "\n".join(
                    f"{e['statusCode']}: {e['message']} ({','.join(e['fields'])})"
                    for e in res["errors"]
                )
            else:
                errors = ""

            return DataOperationResult(res.get("id"), res["success"], errors)

        yield from (_convert(res) for res in self.results)


def get_query_operation(
    *,
    sobject: str,
    fields: List[str],
    api_options: Dict,
    context: Any,
    query: str,
    api: DataApi,
) -> BaseQueryOperation:
    if api is DataApi.SMART:
        record_count_response = context.sf.restful(
            f"limits/recordCount?sObjects={sobject}"
        )
        sobject_map = {
            entry["name"]: entry["count"] for entry in record_count_response["sObjects"]
        }
        api = (
            DataApi.BULK
            if sobject in sobject_map and sobject_map[sobject] >= 2000
            else DataApi.REST
        )

    if api is DataApi.BULK:
        return BulkApiQueryOperation(
            sobject=sobject, api_options=api_options, context=context, query=query
        )
    else:
        return RestApiQueryOperation(
            sobject=sobject,
            api_options=api_options,
            context=context,
            query=query,
            fields=fields,
        )


def get_dml_operation(
    *,
    sobject: str,
    operation: DataOperationType,
    fields: List[str],
    api_options: Dict,
    context: Any,
    api: DataApi,
    volume: int,
) -> BaseQueryOperation:
    if api is DataApi.SMART:
        api = (
            DataApi.BULK
            if volume > 2000 or operation is DataOperationType.HARD_DELETE
            else DataApi.REST
        )

    if api is DataApi.BULK:
        api_class = BulkApiDmlOperation
    else:
        api_class = RestApiDmlOperation

    return api_class(
        sobject=sobject,
        operation=operation,
        api_options=api_options,
        context=context,
        fields=fields,
    )
