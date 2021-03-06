"""
Combine EHR datasets to form full data set

 * Create visit_id_mapping_table to create a global visit id then used in other tables to replce visit_occurrence_id

## Notes
Currently the following environment variables must be set:
 * BIGQUERY_DATASET_ID: BQ dataset where combined result is stored (e.g. test_join_ehr_rdr)
 * APPLICATION_ID: GCP project ID (e.g. all-of-us-ehr-dev)
 * GOOGLE_APPLICATION_CREDENTIALS: path to service account key json file (e.g. /path/to/all-of-us-ehr-dev-abc123.json)
"""
import json
import os
import logging

import bq_utils
import resources
import common
from resources import fields_path

BQ_WAIT_TIME = 10
VISIT_ID_MAPPING_TABLE = 'visit_id_mapping_table'

VIST_ID_MAPPING_QUERY_SKELETON = '''
SELECT
    ROW_NUMBER() OVER() as global_visit_id
    ,hpo
    ,visit_occurrence_id as mapping_visit_id
FROM
(%(union_all_blocks)s);
'''

VISIT_ID_HPO_BLOCK = '''
(SELECT
  visit_occurrence_id
  ,"%(hpo)s" as hpo
FROM `%(project_id)s.%(dataset_id)s.%(hpo)s_visit_occurrence`)
'''

VISIT_ID_MAPPING_TABLE_SUBQUERY = '''
( SELECT global_visit_id, hpo, mapping_visit_id
FROM
`%(project_id)s.%(dataset_id)s.%(visit_id_mapping_table)s`
)
visit_id_map ON t.visit_occurrence_id = visit_id_map.mapping_visit_id
AND
visit_id_map.hpo = '%(hpo)s' '''

TABLE_NAMES = ['person', 'visit_occurrence', 'condition_occurrence', 'procedure_occurrence', 'drug_exposure',
               'device_exposure', 'measurement', 'observation', 'death']


def construct_query(table_name, hpos_to_merge, hpos_with_visit, project_id, dataset_id):
    """
    Get union query for CDM table with proper qualifiers and using global_visit_id for visit_occurrence_id
    :param table_name: name of the CDM table
    :param hpos_to_merge: hpos that should be unioned. sufficient condition is existence of person table
    :param hpos_with_visit: hpos that have visit table loaded
    :param project_id: source project name
    :param dataset_id: source dataset name
    :return: the query
    """
    visit_id_mapping_table = VISIT_ID_MAPPING_TABLE
    source_person_id_field = 'person_id'
    json_path = os.path.join(fields_path, table_name + '.json')
    with open(json_path, 'r') as fp:
        visit_id_flag = False
        fields = json.load(fp)
        col_exprs = []
        for field in fields:
            field_name = field['name']
            # field_type = field['type']
            if field_name == 'visit_occurrence_id':
                visit_id_flag = True
                col_expr = 'global_visit_id as visit_occurrence_id'
            # elif field_name.endswith('_id') and not field_name.endswith('concept_id') and field_type == 'integer':
            # not using this because we refer to other ids in some tables which will get overwritten
            elif field_name == table_name + '_id':
                col_expr = 'ROW_NUMBER() OVER() as %(field_name)s ' % locals()
            else:
                col_expr = field_name
            col_exprs.append(col_expr)
        col_expr_str = ',\n  '.join(col_exprs)
        q = 'SELECT\n  '
        q += ',\n  '.join(col_exprs)
        q += '\nFROM'
        q += '\n ('
        q_subquery_blocks = []
        for hpo in hpos_to_merge:
            q_subquery = ' ( SELECT * FROM `%(project_id)s.%(dataset_id)s.%(hpo)s_%(table_name)s` t' % locals()
            if visit_id_flag and hpo in hpos_with_visit:
                q_subquery += '\n LEFT JOIN  '
                q_subquery += VISIT_ID_MAPPING_TABLE_SUBQUERY % locals()
            q_subquery += ')'
            q_subquery_blocks.append(q_subquery)
        if len(q_subquery_blocks) == 0:
            return ""
        q += "\n UNION ALL \n".join(q_subquery_blocks)
        q += ')'
        return q


def query(q, destination_table_id, write_disposition):
    """
    Run query, log any errors encountered
    :param q: SQL statement
    :param destination_table_id: if set, output is saved in a table with the specified id
    :param write_disposition: WRITE_TRUNCATE, WRITE_APPEND or WRITE_EMPTY (default)
    :return: query result
    """
    qr = bq_utils.query(q, destination_table_id=destination_table_id, write_disposition=write_disposition)
    if 'errors' in qr['status']:
        logging.error('== ERROR ==')
        logging.error(str(qr))
    return qr


def create_mapping_table(hpos_with_visit, project_id, dataset_id):
    """ creates the visit mapping table

    :hpos_with_visit: hpos that should be including the visit occurrence table
    :project_id: project with the dataset
    :dataset_id: dataset with the tables
    :returns: string if visit table failed; otherwise none

    """
    # list of hpos with visit table and creating visit id mapping table queries
    visit_hpo_queries = []
    for hpo in hpos_with_visit:
        visit_hpo_queries.append(VISIT_ID_HPO_BLOCK % locals())
    union_all_blocks = '\n UNION ALL'.join(visit_hpo_queries)
    visit_mapping_query = VIST_ID_MAPPING_QUERY_SKELETON % locals()
    logging.info('Loading ' + VISIT_ID_MAPPING_TABLE)
    query_result = query(visit_mapping_query,
                         destination_table_id=VISIT_ID_MAPPING_TABLE,
                         write_disposition='WRITE_TRUNCATE')
    visit_mapping_query_job_id = query_result['jobReference']['jobId']
    incomplete_jobs = bq_utils.wait_on_jobs([visit_mapping_query_job_id])
    if len(incomplete_jobs) == 0:
        query_result = bq_utils.get_job_details(visit_mapping_query_job_id)
        if 'errors' in query_result['status']:
            errors = query_result['status']['errors']
            message = 'Failed to load %s due to the following error(s): %s' % (VISIT_ID_MAPPING_TABLE, errors)
            logging.error(message)
            raise RuntimeError(message)
    else:
        message = 'Failed to load %s. Job with ID %s never completed.' % (VISIT_ID_MAPPING_TABLE, visit_mapping_query_job_id)
        logging.error(message)
        raise RuntimeError(message)


def result_table_for(table_id):
    return 'unioned_ehr_' + table_id


def merge(dataset_id, project_id):
    """merge hpo ehr data

    :dataset_id: source and target dataset
    :project_id: project in which everything happens
    :returns: list of tables generated successfully

    """
    logging.info('Starting merge')
    existing_tables = bq_utils.list_dataset_contents(dataset_id)
    hpos_to_merge = []
    hpos_with_visit = []
    for item in resources.hpo_csv():
        hpo_id = item['hpo_id']
        if hpo_id + '_person' in existing_tables:
            hpos_to_merge.append(hpo_id)
        if hpo_id + '_visit_occurrence' in existing_tables:
            hpos_with_visit.append(hpo_id)
    logging.info('HPOs to merge: %s' % hpos_to_merge)
    logging.info('HPOs with visit_occurrence: %s' % hpos_with_visit)
    create_mapping_table(hpos_with_visit, project_id, dataset_id)

    # before loading [drop and] create all tables to ensure they are set up properly
    for cdm_file_name in common.CDM_FILES:
        cdm_table_name = cdm_file_name.split('.')[0]
        result_table = result_table_for(cdm_table_name)
        bq_utils.create_standard_table(cdm_table_name, result_table, drop_existing=True)

    jobs_to_wait_on = []
    for table_name in common.CDM_TABLES:
        q = construct_query(table_name, hpos_to_merge, hpos_with_visit, project_id, dataset_id)
        logging.info('Merging table: ' + table_name)
        result_table = result_table_for(table_name)
        query_result = query(q, destination_table_id=result_table, write_disposition='WRITE_TRUNCATE')
        query_job_id = query_result['jobReference']['jobId']
        jobs_to_wait_on.append(query_job_id)

    incomplete_jobs = bq_utils.wait_on_jobs(jobs_to_wait_on)
    if len(incomplete_jobs) == 0:
        tables_created = []
        for job_id in jobs_to_wait_on:
            job_details = bq_utils.get_job_details(job_id)
            status = job_details['status']
            table = job_details['configuration']['query']['destinationTable']['tableId']
            if 'errors' in status:
                logging.error('Job ID %s errors: %s' % (job_id, status['errors']))
            else:
                tables_created.append(table)
        return tables_created
    else:
        message = "Merge failed because job id(s) %s did not complete." % incomplete_jobs
        logging.error(message)
        raise RuntimeError(message)


if __name__ == '__main__':
    dataset_id = os.environ['BIGQUERY_DATASET_ID']
    project_id = os.environ['APPLICATION_ID']
    merge(dataset_id, project_id)
