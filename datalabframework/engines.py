import os

import elasticsearch.helpers
import pyspark
from datetime import datetime
from pyspark.sql.functions import date_format

from datalabframework.spark.diff import dataframe_update
from datalabframework.spark.filter import transform as filter_transform
from datalabframework.spark.mapping import transform as mapping_transform
from . import data
from . import logging
from . import params
from . import utils

# purpose of engines
# abstract engine init, data read and data write
# and move this information to metadata

# it does not make the code fully engine agnostic though.

engines = dict()

import sys


# noinspection PyProtectedMember
def func_name():
    return sys._getframe(1).f_code.co_name

class SparkEngine:
    def __init__(self, name, config):
        from pyspark import SparkContext, SparkConf
        from pyspark.sql import SQLContext

        submit_args = ''

        jars = []
        jars += config.get('jars', [])
        if jars:
            submit_jars = ' '.join(jars)
            submit_args = '{} --jars {}'.format(submit_args, submit_jars)

        packages = config.get('packages', [])
        if packages:
            submit_packages = ','.join(packages)
            submit_args = '{} --packages {}'.format(submit_args, submit_packages)

        submit_args = '{} pyspark-shell'.format(submit_args)

        # os.environ['PYSPARK_SUBMIT_ARGS'] = "--packages org.postgresql:postgresql:42.2.5 pyspark-shell"
        os.environ['PYSPARK_SUBMIT_ARGS'] = submit_args
        print('PYSPARK_SUBMIT_ARGS: {}'.format(submit_args))

        conf = SparkConf()
        if 'jobname' in config:
            conf.setAppName(config.get('jobname'))

        rmd = params.metadata()['providers']
        for v in rmd.values():
            if v['service'] == 'minio':
                conf.set("spark.hadoop.fs.s3a.endpoint", 'http://{}:{}'.format(v['hostname'],v.get('port',9000))) \
                    .set("spark.hadoop.fs.s3a.access.key", v['access']) \
                    .set("spark.hadoop.fs.s3a.secret.key", v['secret']) \
                    .set("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
                    .set("spark.hadoop.fs.s3a.path.style.access", True)
                break

        #set master
        conf.setMaster(config.get('master', 'local[*]'))

        #set log level fro spark
        sc = SparkContext(conf=conf)

        # pyspark set log level method
        # (this will not suppress WARN before starting the context)
        sc.setLogLevel("ERROR")

        self._ctx = SQLContext(sc)
        self.info = {'name': name, 'context':'spark', 'config': config}

    def context(self):
        return self._ctx

    def read(self, resource=None, path=None, provider=None, **kargs):
        logger = logging.getLogger()
        md = data.metadata(resource, path, provider)
        if not md:
            logger.error("No metadata")
            return

        #### Read schema info

        # path - append schema date if available
        md['resource']['path'] = resource_path
        md['url'] = data._url(md)

        obj = self._read(md, **kargs)

        #logging
        logdata = {
            'format':md['provider']['format'],
            'service':md['provider']['service'],
            'path':md['resource']['path'],
            'url':md['url']}
        logtype = {'dlf_type':'{}.{}'.format(self.__class__.__name__, func_name())}
        logger.info(logdata, extra=logtype)
        return obj

    def _read(self, md, **kargs):

        pmd = md['provider']
        rmd = md['resource']

        url = md['url']

        cache = pmd.get('read',{}).get('cache', False)
        cache = rmd.get('read',{}).get('cache', cache)

        repartition = pmd.get('read',{}).get('repartition', None)
        repartition = rmd.get('read',{}).get('repartition', repartition)

        coalesce = pmd.get('read',{}).get('coalesce', None)
        coalesce = rmd.get('read',{}).get('coalesce', coalesce)

        mapping = pmd.get('read', {}).get('mapping', None)
        mapping = rmd.get('read', {}).get('mapping', mapping)

        filter = pmd.get('read', {}).get('filter', None)
        filter = rmd.get('read', {}).get('filter', filter)

        # override options on provider with options on resource, with option on the read method
        options = utils.merge(pmd.get('read',{}).get('options',{}), rmd.get('read',{}).get('options',{}))
        options = utils.merge(options, kargs)

        if pmd['service'] in ['sqlite', 'mysql', 'postgres', 'mssql']:
            format = pmd.get('format', 'rdbms')
        else:
            format = pmd.get('format', 'parquet')

        if pmd['service'] in ['local', 'hdfs', 'minio']:
            if format=='csv':
                obj= self._ctx.read.csv(url, **options)
            if format=='json':
                obj= self._ctx.read.option('multiLine',True).json(url, **options)
            if format=='jsonl':
                obj= self._ctx.read.json(url, **options)
            elif format=='parquet':
                obj= self._ctx.read.parquet(url, **options)
        elif pmd['service'] == 'sqlite':
            driver = "org.sqlite.JDBC"
            obj =  self._ctx.read \
                .format('jdbc') \
                .option('url', url)\
                .option("dbtable", rmd['path'])\
                .option("driver", driver)\
                .load(**options)
        elif pmd['service'] == 'mysql':
            driver = "com.mysql.cj.jdbc.Driver"
            obj =  self._ctx.read\
                .format('jdbc')\
                .option('url', url)\
                .option("dbtable", rmd['path'])\
                .option("driver", driver)\
                .option("user",pmd['username'])\
                .option('password',pmd['password'])\
                .load(**options)
        elif pmd['service'] == 'postgres':
            driver = "org.postgresql.Driver"
            obj =  self._ctx.read\
                .format('jdbc')\
                .option('url', url)\
                .option("dbtable", rmd['path'])\
                .option("driver", driver)\
                .option("user",pmd['username'])\
                .option('password',pmd['password'])\
                .load(**options)
        elif pmd['service'] == 'mssql':
            driver = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
            obj = self._ctx.read \
                .format('jdbc') \
                .option('url', url) \
                .option("dbtable", rmd['path']) \
                .option("driver", driver) \
                .option("user", pmd['username']) \
                .option('password', pmd['password']) \
                .load(**options)
        elif pmd['service'] == 'oracle':
            driver = "oracle.jdbc.driver.OracleDriver"
            obj = self._ctx.read \
                .format('jdbc') \
                .option('url', url) \
                .option("dbtable", rmd['path']) \
                .option("driver", driver) \
                .load(**options)
        elif pmd['service'] == 'elastic':
            obj = self.elastic_read(url, options.get('query', {}))
        else:
            return None

        obj = mapping_transform(obj, mapping) if mapping else obj
        obj = filter_transform(obj,filter) if filter else obj
        obj = obj.repartition(repartition) if repartition else obj
        obj = obj.coalesce(coalesce) if coalesce else obj
        obj = obj.cache() if cache else obj

        return obj

    def write(self, obj, resource=None, path=None, provider=None, **kargs):
        logger = logging.getLogger()

        md = data.metadata(resource, path, provider)
        if not md:
            logger.exception("No metadata")
            return

        self._write(obj, md, **kargs)

        logdata = {
            'format':md['provider']['format'],
            'service':md['provider']['service'],
            'path':md['resource']['path'],
            'url':md['url']}
        logtype = {'dlf_type':'{}.{}'.format(self.__class__.__name__, func_name())}
        logger.info(logdata, extra=logtype)

        return obj

    def _write(self, obj, md, **kargs):
        logger = logging.getLogger()

        pmd = md['provider']
        rmd = md['resource']

        url = md['url']

        cache = pmd.get('write',{}).get('cache', False)
        cache = rmd.get('write',{}).get('cache', cache)

        repartition = pmd.get('write',{}).get('repartition', None)
        repartition = rmd.get('write',{}).get('repartition', repartition)

        coalesce = pmd.get('write',{}).get('coalesce', None)
        coalesce = rmd.get('write',{}).get('coalesce', coalesce)

        mapping = pmd.get('write', {}).get('mapping', None)
        mapping = rmd.get('write', {}).get('mapping', mapping)

        filter = pmd.get('write', {}).get('filter', None)

        # override options on provider with options on resource, with option on the read method
        options = utils.merge(pmd.get('write',{}).get('options',{}), rmd.get('write',{}).get('options',{}))
        options = utils.merge(options, kargs)

        obj = obj.cache() if cache else obj
        obj = obj.coalesce(coalesce) if coalesce else obj
        obj = obj.repartition(repartition) if repartition else obj
        obj = mapping_transform(obj, mapping) if mapping else obj
        obj = filter_transform(obj, mapping) if mapping else obj

        if pmd['service'] in ['sqlite', 'mysql', 'postgres', 'mssql']:
            format = pmd.get('format', 'rdbms')
        else:
            format = pmd.get('format', 'parquet')

        if pmd['service'] in ['local', 'hdfs', 'minio']:
            if format=='csv':
                obj.write.csv(url, **options)
            if format=='json':
                obj.write.option('multiLine',True).json(url, **options)
            if format=='jsonl':
                obj.write.json(url, **options)
            elif format=='parquet':
                obj.write.parquet(url, **options)
            else:
                logger.info('format unknown')

        elif pmd['service'] == 'sqlite':
            driver = "org.sqlite.JDBC"
            obj.write\
                .format('jdbc')\
                .option('url', url)\
                .option("dbtable", rmd['path'])\
                .option("driver", driver)\
                .save(**kargs)

        elif pmd['service'] == 'mysql':
            driver = "com.mysql.cj.jdbc.Driver"
            obj.write\
                .format('jdbc')\
                .option('url', url)\
                .option("dbtable", rmd['path'])\
                .option("driver", driver)\
                .option("user",pmd['username'])\
                .option('password',pmd['password'])\
                .save(**kargs)

        elif pmd['service'] == 'postgres':
            driver = "org.postgresql.Driver"
            obj.write \
                .format('jdbc') \
                .option('url', url) \
                .option("dbtable", rmd['path']) \
                .option("driver", driver) \
                .option("user", pmd['username']) \
                .option('password', pmd['password']) \
                .save(**kargs)
        elif pmd['service'] == 'oracle':
            driver = "oracle.jdbc.driver.OracleDriver"
            obj.write \
                .format('jdbc') \
                .option('url', url) \
                .option("dbtable", rmd['path']) \
                .option("driver", driver) \
                .save(**kargs)
        elif pmd['service'] == 'elastic':
            uri = 'http://{}:{}'.format(pmd["hostname"], pmd["port"])
            mode = kargs.get("mode", None)
            elastic_write(obj, uri, mode, rmd["path"], options["settings"], options["mappings"])
        else:
            raise('downt know how to handle this')


    def elastic_read(self, url, query):
        results = elastic_read(url, query)
        rows = [pyspark.sql.Row(**r) for r in results]
        return self.context().createDataFrame(rows)

    def ingest(self, src_resource=None, src_path=None, src_provider=None,
                     dest_resource=None, dest_path=None, dest_provider=None,
                     eventsourcing=False):

        logger = logging.getLogger()

        #### contants:
        now = datetime.now()
        reserved_cols = ['_ingested','_date','_state']

        #### Source metadata:
        md_src = data.metadata(src_resource, src_path, src_provider)
        if not md_src:
            logger.error("No metadata")
            return

        # filter settings from src (provider and resource)
        filter = utils.merge(
                    md_src['provider'].get('read', {}).get('filter', {}),
                    md_src['resource'].get('read', {}).get('filter', {}))

        #### Target metadata:

        # default path for destination is src path
        if (not dest_resource) and (not dest_path) and dest_provider:
            dest_path = md_src['resource']['path']

        md_dest = data.metadata(dest_resource, dest_path, dest_provider)
        if not md_dest:
            return

        if 'read' not in md_dest['resource']:
            md_dest['resource']['read'] = {}

        # match filter with the one from source resource
        md_dest['resource']['read']['filter'] = filter

        #### Read source resource
        try:
            df_src = self._read(md_src)
        except Exception as e:
            logger.exception(e)
            return

        #### Read schema info

        # destination path - append schema date
        dest_path = '{}/{}'.format(md_dest['resource']['path'], schema_date_str)
        md_dest['resource']['path'] = dest_path
        md_dest['url'] = data._url(md_dest)

        # if schema not present or schema change detected
        schema_changed = True

        if schema_changed:
            #Different schema, update schema table with new entry
            schema_entry = (schema_date_str, now, df_src.schema.json())
            df_schema = self.context().createDataFrame([schema_entry],['id', 'date', 'schema'])

            # write the schema to destination provider
            md = data.metadata(path=schema_path, provider=md_dest['resource']['provider'])
            self._write(df_schema, md, mode='append')

        # partitions
        partition_cols = ['_ingested']
        
        if not eventsourcing:
            if filter.get('policy')=='date' and filter.get('column'):
                 df_diff = dataframe_update(df_src, df_dest, updated_col='_ingested', eventsourcing=eventsourcing)
                 df_diff = df_diff.withColumn('_date', date_format(filter['column'], 'yyyy-MM-dd'))
                 partition_cols += ['_date']
                 ingest_mode = 'append'
                 options = {'mode':ingest_mode, 'partitionBy':partition_cols}
            else:
                 df_diff = dataframe_update(df_src, df_dest.filter("False"), updated_col='_ingested', eventsourcing=eventsourcing)
                 ingest_mode = 'overwrite'
                 options = {'mode':ingest_mode, 'partitionBy':partition_cols}
        else:
            # to do
            logger.fatal('event sourcing not implemented yet')

        records_add = df_diff.filter("_state = 0").count()
        records_del = df_diff.filter("_state = 1").count()

        if records_add or records_del or schema_changed:
            md = data.metadata(path=dest_path, provider=md_dest['resource']['provider'])
            self._write(df_diff, md, **options)

        end = datetime.now()
        time_diff = end - now

        logdata = {
            'src_url': md_src['url'],
            'src_table': md_src['resource']['path'],
            'source_option': filter,
            'schema_change': schema_changed,
            'target': dest_path,
            'upserts': records_add,
            'deletes': records_del,
            'diff_time': time_diff.total_seconds()}

        logtype = {'dlf_type':'{}.{}'.format(self.__class__.__name__, func_name())}
        logger.info(logdata, extra=logtype)

def elastic_read(url, query):
    """
    :param url:
    :param query:
    :return: python list of dict

    sample resource:
    variables:
        size: 10
        from: 0
        query: "laptop"
    keywords_search:
        provider: elastic_test
        path: /search_keywords_dev/keyword/
        query: >
            {
              "size" : "{{ default.variables.size }}",
              "from" : "{{ default.variables.size }}",
              "query": {
                "function_score": {
                  "query": {
                      "match" : {
                        "query_wo_tones": {"query":"{{ default.variables.query }}", "operator" :"or", "fuzziness":"AUTO"}
                    }
                  },
                  "script_score" : {
                      "script" : {
                        "source": "Math.sqrt(doc['impressions'].value)"
                      }
                  },
                  "boost_mode":"multiply"
                }
              }
            }
    """

    try:
        es = elasticsearch.Elasticsearch([url])
        res = es.search(body=query)
    except:
        raise ValueError("Cannot query elastic search")

    hits = res.pop("hits", None)
    if hits is None:
        #handle ERROR
        raise ValueError("Query failed")

        # res["total_hits"] = hits["total"]
        # res["max_score"] = hits["max_score"]
        # print("Summary:", res)

    hits2 = []
    for hit in hits['hits']:
        hitresult = hit.pop("_source", None)
        hits2.append({**hit, **hitresult})

    return hits2


def elastic_write(obj, uri, mode='append', indexName=None, settings=None, mappings=None):
    """
    :param mode: overwrite | append
    :param obj: spark dataframe, or list of Python dictionaries
    :return:
    """
    es = elasticsearch.Elasticsearch([uri])
    if mode == "overwrite":
            # properties:
            #                 {
            #                     "count": {
            #                         "type": "integer"
            #                     },
            #                     "keyword": {
            #                         "type": "keyword"
            #                     },
            #                     "query": {
            #                         "type": "text",
            #                         "fields": {
            #                             "keyword": {
            #                                 "type": "keyword",
            #                                 "ignore_above": 256
            #                             }
            #                         }
            #                     }
            #                 }
            # OR:
            # properties:
            #     count: integer
            #     keyword: keyword
            #     query:
            #         type: text
            #         fields:
            #             keyword:
            #                 type: keyword
            #                 ignore_above: 256
            #     query_wo_tones:
            #         type: text
            #         fields:
            #             keyword:
            #                 type: keyword
            #                 ignore_above: 256
        for k, v in mappings["properties"].items():
            if isinstance(mappings["properties"][k], str):
                mappings["properties"][k] = {"type":mappings["properties"][k]}
            # settings:
            #         {
            #             "index": {
            #                 "number_of_shards": 1,
            #                 "number_of_replicas": 3,
            #                 "mapping": {
            #                     "total_fields": {
            #                         "limit": "1024"
            #                     }
            #                 }
            #             }
            #         }
            # OR:
            # settings:
            #     index:
            #         number_of_shards: 1
            #         number_of_replicas: 3
            #         mapping:
            #             total_fields:
            #                 limit: 1024

        print(settings)
        print(mappings["properties"])

        if not settings or not settings:
            raise ("'settings' and 'mappings' are required for 'overwrite' mode!")
        es.indices.delete(index=indexName, ignore=404)
        es.indices.create(index=indexName, body={
            "settings": settings,
            "mappings": {
                mappings["doc_type"]: {
                    "properties": mappings["properties"]
                }
            }
        })
    elif mode == "append": # append
        pass
    else:
        raise ValueError("Unsupported mode: " + mode)

    # import pandas.core.frame
    # import pyspark.sql.dataframe
    # import numpy as np
    # if isinstance(obj, pandas.core.frame.DataFrame):
    #     obj = obj.replace({np.nan:None}).to_dict(orient='records')
    # elif isinstance(obj, pyspark.sql.dataframe.DataFrame):
    #     obj = obj.toPandas().replace({np.nan:None}).to_dict(orient='records')
    # else: # python list of python dictionaries
    #     pass

    if isinstance(obj, pyspark.sql.dataframe.DataFrame):
        obj = [row.asDict() for row in obj.collect()]
    else: # python list of python dictionaries
        pass

    from collections import deque
    deque(elasticsearch.helpers.parallel_bulk(es, obj, index=indexName,
                                              doc_type=mappings["doc_type"]), maxlen=0)
    es.indices.refresh()


def get(name):
    global engines

    #get
    engine = engines.get(name)

    if not engine:
        #create
        md = params.metadata()
        cn = md['engines'].get(name)
        config = cn.get('config', {})

        if cn['context']=='spark':
            engine = SparkEngine(name, config)
            engines[name] = engine

    return engine
