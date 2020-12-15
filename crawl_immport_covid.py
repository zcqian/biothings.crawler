import datetime
import multiprocessing
import os
import uuid
import sys
import yaml
import argparse
from typing import Optional

os.chdir(os.path.abspath(os.path.dirname(__file__)))
# patch PATH so local venv is in PATH
bin_path = os.path.join(os.getcwd(), 'venv/bin')
os.environ['PATH'] += os.pathsep + bin_path
# patch PATH so interpreter dir is also in PATH
os.environ['PATH'] += os.pathsep + \
                      os.path.abspath(os.path.dirname(sys.executable))

from scrapy.spiderloader import SpiderLoader
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from elasticsearch import Elasticsearch

from crawler.upload import uploaders


def get_build_timestamp(es: Elasticsearch, alias_name: str) -> Optional[int]:
    try:
        om = es.indices.get_mapping(alias_name, include_type_name=False)
        if len(om.keys()) != 1:
            raise ValueError()
        o_idx_name = list(om.keys())[0]
        orig_build_date = om[o_idx_name]['mappings']['_meta']['build_date']
        orig_build_date = datetime.datetime.fromisoformat(orig_build_date)
        # check timezone
        if orig_build_date.tzinfo is None:
            orig_build_date.astimezone()  # force to local timezone
            # anecdote: if timezones are properly configured, should handle
            # dst properly
        orig_build_date = int(orig_build_date.timestamp())
    except:
        orig_build_date = None
    return orig_build_date


def invoke_crawl(es_host: str, es_index: str, crawler: str):
    # crawler uses env vars for this
    os.environ['ES_HOST'] = es_host
    os.environ['ES_INDEX'] = es_index
    # crawl
    process = CrawlerProcess(get_project_settings())
    process.crawl(crawler)
    process.start()
    process.join()


def alias_switcheroo(es: Elasticsearch, alias_name: str, index_name: str):
    # alias update
    if not es.indices.exists(alias_name):
        es.indices.put_alias(index=index_name, name=alias_name)
    else:
        # if an index not alias exists, just let it crash
        actions = {
            "actions": [
                {"add": {"index": index_name, "alias": alias_name}}
            ]
        }
        rm_idx = [i_name for i_name in es.indices.get_alias(alias_name)]
        removes = [{
            "remove": {"index": index_name, "alias": alias_name}
        } for index_name in rm_idx
        ]
        actions["actions"].extend(removes)
        es.indices.update_aliases(actions)
        # delete old indices
        for rm_i in rm_idx:
            es.indices.delete(rm_i)


def perform_crawl_and_update(
        crawler: str, uploader: str, alias_name: str,
        es_host_c: str, es_host_u: str,
        es_idx_c: Optional[str] = None,
        es_idx_u: Optional[str] = None,
):
    es_crawler = Elasticsearch(es_host_c)
    es_uploader = Elasticsearch(es_host_u)
    if es_idx_c is None or es_idx_u is None:
        flag = True
        while flag:
            u = uuid.uuid1()
            tmp_idx_c = f"crawler_immport_covid_{u.hex}"
            tmp_idx_u = f"uploader_immport_covid_{u.hex}"
            flag1 = es_crawler.indices.exists(tmp_idx_c) and es_idx_c is None
            flag2 = es_uploader.indices.exists(tmp_idx_u) and es_idx_u is None
            flag = flag1 or flag2  # both idx names: set or not already exist
        if es_idx_u is None:
            es_idx_u = tmp_idx_u
        if es_idx_c is None:
            es_idx_c = tmp_idx_c
    # crawl
    invoke_crawl(es_host_c, es_idx_c, crawler)
    # force a refresh, might cause performance issues
    # will change this if that happens
    es_crawler.indices.refresh(index=es_idx_c)
    # upload
    uploader = uploaders[uploader](
        src_host=es_host_c,
        src_index=es_idx_c,
        dest_host=es_host_u,
        dest_index=es_idx_u
    )
    uploader.upload()
    # update alias
    alias_switcheroo(es_uploader, alias_name, es_idx_u)


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn')
    # load environment
    scrapy_settings = get_project_settings()
    spiders = SpiderLoader.from_settings(scrapy_settings).list()

    # handle arguments
    parser = argparse.ArgumentParser(description="Crawl and update a source")
    subparsers = parser.add_subparsers(dest='action')
    runyaml_parser = subparsers.add_parser('runyaml', help='run from YAML doc')
    runyaml_parser.add_argument('--yaml', required=True)
    run_parser = subparsers.add_parser('runcmd', help='run from command line')
    run_parser.add_argument('--crawler', '-c',
                            type=str, choices=spiders, required=True)
    run_parser.add_argument('--uploader', '-u',
                            type=str, choices=uploaders.keys(), required=True)
    run_parser.add_argument('--es-host-crawler', '-ehc',
                            type=str, default='localhost')
    run_parser.add_argument('--es-index-crawler', '-eic', type=str,
                            help="""Index name for crawler to use. If omitted,
                        a proper random name will be chosen.
                        This index is deleted after the uploader has completed
                        running.""")
    run_parser.add_argument('--es-host-uploader', '-ehu',
                            type=str, default='localhost')
    run_parser.add_argument('--es-index-uploader', '-eiu', type=str,
                            help="""Index name for uploader to use. If omitted,
                        a proper random name will be chosen. This index is kept
                        until next successful run.""")
    run_parser.add_argument('--target-alias', '-a', type=str, required=True,
                            help="""Target alias""")
    args = parser.parse_args()

    tasks = {}
    if args.action == 'runyaml':
        with open(args.yaml) as f:
            config = yaml.load(f, Loader=yaml.FullLoader)
        for k, v in config.items():
            task = {
                'crawler': v['crawler'],
                'uploader': v['uploader'],
                'es_host_c': v['crawler_host'],
                'es_host_u': v['uploader_host'],
                'es_idx_c': v.get('crawler_index'),
                'es_idx_u': v.get('uploader_index'),
                'alias_name': v['alias_name'],
            }
            tasks[k] = task

    elif args.action == 'runcmd':
        tasks['cmdline'] = {
                'crawler': args.crawler,
                'uploader': args.uploader,
                'es_host_c': args.es_host_crawler,
                'es_host_u': args.es_host_uploader,
                'es_idx_c': args.es_index_crawler,
                'es_idx_u': args.es_index_uploader,
                'alias_name': args.target_alias,
        }
    else:
        pass

    for k, v in tasks.items():
        p = multiprocessing.Process(target=perform_crawl_and_update,
                                    kwargs=v)
        print(f"Executing {k} ...")
        p.start()
        p.join()

