# coding=utf-8
# Author: Claus Michele - Eurac Research - michele (dot) claus (at) eurac (dot) edu
# Date:   23/02/2023

import os
import signal
import sys
from flask import Flask, request, jsonify
import json
import requests
import yaml
import pandas as pd
import time
import logging
import hashlib
import uuid

from openeo_odc_driver import ProcessOpeneoGraph
from sar2cube.utils import sar2cube_collection_extent
from config import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("odc_backend.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

_log = logging.getLogger(__name__)

app = Flask(FLASK_APP_NAME)

@app.errorhandler(500)
def error500(error):
    return error, 500

@app.errorhandler(400)
def error400(error):
    return error, 400

@app.route('/graph', methods=['POST'])
def process_graph():
    if not os.path.exists(JOB_LOG_FILE):
        lst = ['job_id', 'pid', 'creation_time']
        df = pd.DataFrame(columns=lst)
        df.to_csv(JOB_LOG_FILE)
    else:
        df = pd.read_csv(JOB_LOG_FILE,index_col=0)

    jsonGraph = request.json

    _log.debug('Gunicorn worker pid for this job: {}'.format(os.getpid()))
    try:
        job_id = jsonGraph['id']
    except Exception as e:
        _log.error(e)
        job_id = 'None'
            
    pg = jsonGraph["process_graph"]
    m = hashlib.md5()
    m.update(str(pg).encode('utf8'))
    hex_string = str(m.digest())

    if not os.path.exists(JOB_CACHE_FILE):
        lst = ['hex_string', 'path']
        df_cache = pd.DataFrame(columns=lst)
        df_cache.to_csv(JOB_CACHE_FILE)
    else:
        df_cache = pd.read_csv(JOB_CACHE_FILE,index_col=0)
        if len(df_cache.loc[df_cache['hex_string']==hex_string]['path'].values)>0:
            path = df_cache.loc[df_cache['hex_string']==hex_string]['path'].values[0]
            _log.debug("CACHE PATH " + path)
            filename = path.split('/')[-1]
            if os.path.exists(RESULT_FOLDER_PATH + '/' + path):
                if job_id == "None":
                    job_id = str(uuid.uuid4())
                result_folder_path = RESULT_FOLDER_PATH + job_id # If it is a batch job, there will be a field with it's id
                if not os.path.exists(result_folder_path):
                    os.mkdir(result_folder_path)
                from shutil import copyfile
                copyfile(RESULT_FOLDER_PATH + '/' + path, result_folder_path + '/' + filename)
                _log.debug("NEW PATH " + result_folder_path + '/' + filename)
                return jsonify({'output':job_id + '/' + filename})        
    try:
        current_time = time.localtime()
        time_string = time.strftime('%Y-%m-%dT%H%M%S', current_time)
        
        df = df[df['job_id']!=job_id]
        df = df.append({'job_id':job_id,'creation_time':time_string,'pid':os.getpid()},ignore_index=True)
        df.to_csv(JOB_LOG_FILE)
        
        eo = ProcessOpeneoGraph(jsonGraph)
        
        df_cache = df_cache[df_cache['hex_string']!=hex_string]
        df_cache = df_cache.append({'hex_string':hex_string,'path':eo.result_folder_path.split('/')[-1] + '/result'+eo.out_format},ignore_index=True)
        df_cache.to_csv(JOB_CACHE_FILE)
        
        return jsonify({'output':eo.result_folder_path.split('/')[-1] + '/result'+eo.out_format})
    except Exception as e:
        _log.error(e)
        return error400('ODC engine error in process: ' + str(e))
    
@app.route('/stop_job', methods=['DELETE'])
def stop_job():
    try:
        job_id = request.args['id']
        _log.debug('Job id to cancel: {}'.format(job_id))
        if os.path.exists(JOB_LOG_FILE):
            df = pd.read_csv(JOB_LOG_FILE,index_col=0)
            pid = df.loc[df['job_id']==job_id]['pid'].values[0]
            _log.debug('Job PID to stop: {}'.format(pid))
            os.kill(pid, signal.SIGINT)
            df = df[df['job_id']!=job_id]
            df.to_csv(JOB_LOG_FILE)
        return jsonify('ok'), 204
    except Exception as e:
        _log.error(e)
        return error400(str(e))

@app.route('/collections', methods=['GET'])
def list_collections():
    if USE_CACHED_COLLECTIONS:
        if os.path.isfile(METADATA_COLLECTIONS_FILE):
            f = open(METADATA_COLLECTIONS_FILE)
            with open(METADATA_COLLECTIONS_FILE) as collection_list:
                stacCollection = json.load(collection_list)
                return jsonify(stacCollection)
    res = requests.get(DATACUBE_EXPLORER_ENDPOINT + "/products.txt")
    collections = {}
    collections['collections'] = []
    if (not res.text.strip()):
        logging.info("No products exposed by the ODC explorer.")
    else:
        datacubesList = res.text.split('\n')
        collectionsList = []
        for i,d in enumerate(datacubesList):
            currentCollection = construct_stac_collection(d)
            collectionsList.append(currentCollection)
        collections['collections'] = collectionsList
        with open(METADATA_COLLECTIONS_FILE, 'w') as outfile:
            json.dump(collections, outfile)

    return jsonify(collections)


@app.route("/collections/<string:name>", methods=['GET'])
def describe_collection(name):
    if not os.path.exists(METADATA_CACHE_FOLDER):
        os.mkdir(METADATA_CACHE_FOLDER)
    if USE_CACHED_COLLECTIONS:
        if os.path.isfile(METADATA_CACHE_FOLDER + '/' + name + '.json'):
            f = open(METADATA_CACHE_FOLDER + '/' + name + '.json')
            with open(METADATA_CACHE_FOLDER + '/' + name + '.json') as collection:
                stacCollection = json.load(collection)
                return jsonify(stacCollection)

    stacCollection = construct_stac_collection(name)

    return jsonify(stacCollection)

def construct_stac_collection(collectionName):
    logging.info("[*] Constructing the metadata for {}".format(collectionName))
    if not os.path.exists(METADATA_CACHE_FOLDER):
        os.mkdir(METADATA_CACHE_FOLDER)
    if USE_CACHED_COLLECTIONS:
        if os.path.isfile(METADATA_CACHE_FOLDER + '/' + collectionName + '.json'):
            f = open(METADATA_CACHE_FOLDER + '/' + collectionName + '.json')
            with open(METADATA_CACHE_FOLDER + '/' + collectionName+ '.json') as collection:
                stacCollection = json.load(collection)
                return stacCollection

    res = requests.get(DATACUBE_EXPLORER_ENDPOINT + "/collections/" + collectionName)
    stacCollection = res.json()
    metadata = None
    if not os.path.exists(METADATA_SUPPLEMENTARY_FOLDER):
        os.mkdir(METADATA_SUPPLEMENTARY_FOLDER)
    if os.path.isfile(METADATA_SUPPLEMENTARY_FOLDER + '/' + collectionName + '.json'):
        additional_metadata = open(METADATA_SUPPLEMENTARY_FOLDER + '/' + collectionName + '.json')
        metadata = json.load(additional_metadata)

    stacCollection['stac_extensions'] = ['datacube']
    stacCollection['license'] = DEFAULT_DATA_LICENSE
    stacCollection['providers'] = [DEFAULT_DATA_PROVIDER]
    stacCollection['links'] = [DEFAULT_LINKS]
    
    if "SAR2Cube" in collectionName:
        try:
            sar2cubeBbox = sar2cube_collection_extent(collectionName)
            stacCollection['extent']['spatial']['bbox'] = [sar2cubeBbox]
        except Exception as e:
            logging.error(e)
            pass

    ### SUPPLEMENTARY METADATA FROM FILE
    if metadata is not None:
        if 'extent' in metadata.keys():
            if 'temporal' in metadata['extent']:
                stacCollection['extent']['temporal'] = metadata['extent']['temporal']
        if 'title' in metadata.keys():
            stacCollection['title']        = metadata['title']
        if 'description' in metadata.keys():
            stacCollection['description']  = metadata['description']
        if 'keywords' in metadata.keys():
            stacCollection['keywords']     = metadata['keywords']
        if 'providers' in metadata.keys():
            stacCollection['providers']    = metadata['providers']
        if 'version' in metadata.keys():
            stacCollection['version']      = metadata['version']
        if 'deprecated' in metadata.keys():
            stacCollection['deprecated']   = metadata['deprecated']
        if 'license' in metadata.keys():
            stacCollection['license']      = metadata['license']
        if 'sci:citation' in metadata.keys():
            stacCollection['sci:citation'] = metadata['sci:citation']
            stacCollection['stac_extensions'] = ['datacube','scientific']
        if 'links' in metadata.keys():
            stacCollection['links']        = metadata['links']
        if 'summaries' in metadata.keys():
            stacCollection['summaries'] = {}
            if 'rows' in metadata['summaries']:
                stacCollection['summaries']['rows']           = metadata['summaries']['rows']
            if 'columns' in metadata['summaries']:
                stacCollection['summaries']['columns']        = metadata['summaries']['columns']
            if 'gsd' in metadata['summaries']:
                stacCollection['summaries']['gsd']            = metadata['summaries']['gsd']
            if 'constellation' in metadata['summaries']:
                stacCollection['summaries']['constellation']  = metadata['summaries']['constellation']
            if 'platform' in metadata['summaries']:
                stacCollection['summaries']['platform']       = metadata['summaries']['platform']
            if 'instruments' in metadata['summaries']:
                stacCollection['summaries']['instruments']    = metadata['summaries']['instruments']
            if 'eo:cloud cover' in metadata['summaries']:
                stacCollection['summaries']['eo:cloud cover'] = metadata['summaries']['eo:cloud cover']
        if 'cube:dimensions' in metadata.keys():
            if DEFAULT_BANDS_DIMENSION_NAME in metadata['cube:dimensions'].keys():
                if 'values' in metadata['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME].keys():
                    stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME] = {}
                    stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME]['type'] = 'bands'
                    stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME]['values'] = metadata['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME]['values']

    ### SPATIAL AND TEMPORAL EXTENT FROM DATACUBE-EXPLORER
    stacCollection['cube:dimensions'] = {}
    stacCollection['cube:dimensions'][DEFAULT_TEMPORAL_DIMENSION_NAME] = {}
    stacCollection['cube:dimensions'][DEFAULT_TEMPORAL_DIMENSION_NAME]['type'] = 'temporal'
    stacCollection['cube:dimensions'][DEFAULT_TEMPORAL_DIMENSION_NAME]['extent'] = stacCollection['extent']['temporal']['interval'][0]

    stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME] = {}
    stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['type'] = 'spatial'
    stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['axis'] = 'x'
    stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['extent'] = [stacCollection['extent']['spatial']['bbox'][0][0],stacCollection['extent']['spatial']['bbox'][0][2]]

    stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME] = {}
    stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['type'] = 'spatial'
    stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['axis'] = 'y'
    stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['extent'] = [stacCollection['extent']['spatial']['bbox'][0][1],stacCollection['extent']['spatial']['bbox'][0][3]]

    res = requests.get(DATACUBE_EXPLORER_ENDPOINT + "/collections/" + collectionName + "/items")
    items = res.json()


    ## TODO: remove this part when all the datacubes have a metadata file, crs comes from there
    try:
        if 'location' in items['features'][0]['assets']:
            yamlFile = items['features'][0]['assets']['location']['href']
            yamlFile = yamlFile.split('file://')[1].replace('%40','@').replace('%3A',':')

            with open(yamlFile, 'r') as stream:
                try:
                    yamlDATA = yaml.safe_load(stream)
                    stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['reference_system'] = int(yamlDATA['grid_spatial']['projection']['spatial_reference'].split('EPSG')[-1].split('\"')[-2])
                    stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['reference_system'] = int(yamlDATA['grid_spatial']['projection']['spatial_reference'].split('EPSG')[-1].split('\"')[-2])
                except Exception as e:
                    print(e)
        else:
            stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['reference_system'] = 4326
            stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['reference_system'] = 4326
    except:
        pass

    if metadata is not None:
        if 'crs' in metadata.keys():
            stacCollection['cube:dimensions'][DEFAULT_X_DIMENSION_NAME]['reference_system'] = metadata['crs']
            stacCollection['cube:dimensions'][DEFAULT_Y_DIMENSION_NAME]['reference_system'] = metadata['crs']

    ### BANDS FROM DATACUBE-EXPLORER IF NOT ALREADY PROVIDED IN THE SUPPLEMENTARY METADATA
    bands_list = []
    try:
        keys = items['features'][0]['assets'].keys()
        list_keys = list(keys)
        if 'location' in list_keys: list_keys.remove('location')
        try:
            for key in list_keys:
                if 'eo:bands' in items['features'][0]['assets'][key]:
                    for b in items['features'][0]['assets'][key]['eo:bands']:
                        name = b
                        # odc explorer different outputs on different versions:
                        if type(b) is dict:
                            assert "name" in b
                            name = b["name"]
                        bands_list.append(name)
            stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME] = {}
            stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME]['type'] = 'bands'
            stacCollection['cube:dimensions'][DEFAULT_BANDS_DIMENSION_NAME]['values'] = bands_list
        except Exception as e:
            print(e)
    except Exception as e:
        print(e)

    with open(METADATA_CACHE_FOLDER + '/' + collectionName + '.json', 'w') as outfile:
        json.dump(stacCollection, outfile)
    return stacCollection
