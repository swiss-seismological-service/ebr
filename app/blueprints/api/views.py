
from datamodel.asset import PostalCode
from flask import jsonify, make_response, request
from sqlalchemy import func, distinct

import pandas as pd
import io
import requests
import time
from datetime import datetime
import threading

from openquake.calculators.extract import Extractor

from . import api
from app.extensions import csrf

from datamodel import (session, engine, AssetCollection, Asset, Site,
                       VulnerabilityFunction, VulnerabilityModel, LossConfig,
                       LossModel, LossCalculation, MeanAssetLoss, Municipality)

from .utils import create_exposure_csv, create_exposure_xml, create_file_pointer, create_risk_ini, create_hazard_ini, create_vulnerability_xml, ini_to_dict, sites_from_assets
from .parsers import parse_oq_exposure_file, parse_oq_vulnerability_file, parse_asset_csv, risk_dict_to_lossmodel_dict

# @api.route('/')
# def index():
#     test.apply_async()
#     return 'Hello World'


@api.get('/assetcollection')
@csrf.exempt
def get_exposure():
    """ /api/v1/assetcollection 
    get:
        summary: Endpoint for AssetCollection
        description: Get all AssetCollections including Asset and Site counts
        parameters: 
            - None
        responses:
            200:
                type: application/json
                schema: AssetCollection
                extra fields:
                    - name: assets_count
                    - type: integer
                    - name: sites_count
                    - type: integer
    """
    # query exposure models and number of Assets and Sites
    asset_collection = session \
        .query(AssetCollection, func.count(distinct(Asset._oid)),
               func.count(distinct(Site._oid))) \
        .select_from(AssetCollection) \
        .outerjoin(Asset) \
        .outerjoin(Site).group_by(AssetCollection._oid).all()

    # assemble response object
    response = []
    for collection in asset_collection:
        collection_dict = collection[0]._asdict()
        collection_dict['assets_count'] = collection[1]
        collection_dict['sites_count'] = collection[2]

        response.append(collection_dict)

    return make_response(jsonify(response), 200)


@api.post('/assetcollection')
@csrf.exempt
def post_exposure():
    """ /api/v1/assetcollection 
    post:
        summary: Endpoint for AssetCollection
        description: Post an AssetCollection and corresponding Sites and Assets
        consumes: multipart/form-data
        parameters: 
            - name: exposureXML
              in: body
              type: file
              description: OpenQuake exposure_file XML
            - name: exposureCSV
              in: body
              type: file
              description: OpenQuake assets csv
        responses:
            400: 
                description: bad request
            200:
                description: OK, returns object
                type: application/json
                schema: AssetCollection
                extra fields:
                    - name: assets_count
                      type: integer
                    - name: sites_count
                      type: integer
    """
    # read xml file
    file = request.files.get('exposureXML')
    model = parse_oq_exposure_file(file)
    assetcollection = AssetCollection(**model)

    # read assets into pandas dataframe and rename columns
    file_assets = request.files.get('exposureCSV')
    assets_df = parse_asset_csv(file_assets)

    # add tags to session and tag names to Asset Collection
    assetcollection.tagnames = []
    if '_municipality_oid' in assets_df:
        assetcollection.tagnames.append('municipality')
        for el in assets_df['_municipality_oid'].unique():
            session.merge(Municipality(_oid=el))
    if '_postalcode_oid' in assets_df:
        assetcollection.tagnames.append('postalcode')
        for el in assets_df['_postalcode_oid'].unique():
            session.merge(PostalCode(_oid=el))

    # flush assetcollection to get id
    session.add(assetcollection)
    session.flush()

    # assign assetcollection
    assets_df['_assetcollection_oid'] = assetcollection._oid

    # create sites and assign sites list index to assets
    sites, assets_df['sites_list_index'] = sites_from_assets(
        assets_df)

    # add and flush sites to get an ID but keep fast accessible in memory
    session.add_all(sites)
    session.flush()

    # assign ID back to dataframe using group index
    assets_df['_site_oid'] = assets_df.apply(
        lambda x: sites[x['sites_list_index']]._oid, axis=1)

    # commit so that FK exists in databse
    session.commit()

    # write selected columns directly to database
    assets_df.filter(Asset.get_keys()).to_sql(
        'loss_asset', engine, if_exists='append', index=False)

    return get_exposure()


@api.get('/vulnerabilitymodel')
@csrf.exempt
def get_vulnerability():
    """ /api/v1/vulnerabilitymodel
    get:
        summary: Endpoint for VulnerabilityModel
        description: Get all VulnerabilityModels including VulnerabilityFunctions count
        responses:
            200:
                description: OK, returns object
                type: application/json
                schema: VulnerabilityModel
                extra fields:
                    - name: functions_count
                      type: integer 
    """
    # query vulnerability Models and number of functions
    vulnerability_model = session.query(VulnerabilityModel,
                                        func.count(VulnerabilityFunction._oid)) \
        .outerjoin(VulnerabilityFunction) \
        .group_by(VulnerabilityModel._oid).all()

    # assemble response object
    response = []
    for model in vulnerability_model:
        model_dict = model[0]._asdict()
        model_dict['functions_count'] = model[1]

        response.append(model_dict)

    return make_response(jsonify(response), 200)


@api.post('/vulnerabilitymodel')
@csrf.exempt
def post_vulnerability():
    """ /api/v1/vulnerabilitymodel 
    post:
        summary: Endpoint for VulnerabilityModel
        description: Post a VulnerabilityModel
        consumes: multipart/form-data
        parameters: 
            - name: vulnerabilitymodel
              in: body
              type: file
              description: OpenQuake vulnerability_file xml
        responses:
            400: 
                description: bad request
            200:
                description: OK, returns object
                type: application/json
                schema: VulnerabilityModel
                extra fields:
                    - name: functions_count
                      type: integer 
    """

    # read xml file
    file = request.files.get('vulnerabilitymodel')
    model, functions = parse_oq_vulnerability_file(file)

    # assemble vulnerability Model
    vulnerabilitymodel = VulnerabilityModel(**model)
    session.add(vulnerabilitymodel)
    session.flush()

    # assemble vulnerability Functions
    for vF in functions:
        f = VulnerabilityFunction(**vF)
        f._vulnerabilitymodel_oid = vulnerabilitymodel._oid
        session.add(f)

    session.commit()

    return get_vulnerability()


@api.get('/lossmodel')
@csrf.exempt
def get_loss_model():
    """ /api/v1/lossmodel
    get:
        summary: Endpoint for LossModel
        description: Get all available LossModels 
        responses:
            200:
                description: OK, returns object
                type: application/json
                schema: LossModel
                extra fields:
                    - name: calculations_count
                      type: integer 
                    - name: _vulnerabilitymodels_oids
                      type: array(integer)
    """
    # query loss models and count related loss calculations
    loss_models = session.query(LossModel,
                                func.count(LossCalculation._oid)) \
        .outerjoin(LossCalculation) \
        .group_by(LossModel._oid).all()

    # assemble response object
    response = []
    for model in loss_models:
        model_dict = model[0]._asdict()
        model_dict['calculations_count'] = model[1]
        model_dict['_vulnerabilitymodels_oids'] = sorted([
            v._oid for v in model[0].vulnerabilitymodels])

        response.append(model_dict)

    return make_response(jsonify(response), 200)


@api.post('/lossmodel')
@csrf.exempt
def post_loss_model():
    """ /api/v1/lossmodel 
    post:
        summary: Endpoint for LossModel
        description: Post a LossModel
        consumes: multipart/form-data
        parameters: 
            - name: riskini
              in: body
              type: file
              description: OpenQuake (risk.ini) job config file
            - name: _assetcollection_oid
              in: body
              type: integer
              description: AssetCollection (Exposure Model) _oid
            - name: _vulnerabilitymodels_oids
              in: body
              type: string
              description: csv string with VulnerabilityModel _oids
        responses:
            400: 
                description: bad request
            200:
                description: OK, returns object
                type: application/json
                schema: LossModel
                extra fields:
                    - name: calculations_count
                      type: integer 
                    - name: _vulnerabilitymodels_oids
                      type: array(integer)
    """
    # read form data and uploaded file
    form_data = request.form
    file = request.files.get('riskini')

    # parse ini file to dict and read relevant fields
    file_data = ini_to_dict(file)
    data = risk_dict_to_lossmodel_dict(file_data)

    # add asset collection id
    data['_assetcollection_oid'] = int(form_data['_assetcollection_oid'])
    data['preparationcalculationmode'] = 'scenario'

    # get vulnerability models
    vmIDs = [
        int(x) for x in form_data['_vulnerabilitymodels_oids'].split(',')]
    vulnerabilitymodels = session.query(VulnerabilityModel).filter(
        VulnerabilityModel._oid.in_(vmIDs)).all()
    data['vulnerabilitymodels'] = vulnerabilitymodels

    # assemble LossModel object
    loss_model = LossModel(**data)
    session.add(loss_model)
    session.commit()

    return get_loss_model()


@api.get('/lossconfig')
@csrf.exempt
def get_loss_config():
    """ /api/v1/lossconfig
    get:
        summary: Endpoint for LossConfig
        description: Get all available LossConfigs 
        responses:
            200:
                description: OK, returns object
                type: application/json
                schema: LossConfig
    """
    loss_config = session.query(LossConfig).all()

    response = []
    for config in loss_config:
        config_dict = config._asdict()

        response.append(config_dict)

    return make_response(jsonify(response), 200)


@api.post('/lossconfig')
@csrf.exempt
def post_loss_config():
    """ /api/v1/lossconfig 
    post:
        summary: Endpoint for LossConfig
        description: Post a LossConfig
        consumes: application/json
        parameters: 
            - name: losscategory
              in: body
              type: string
              description: loss category name
            - name: aggregateby
              in: body
              type: string
              required: false
              description: aggregation key name
            - name: _lossmodel_oid
              in: body
              type: integer
              description: LossModel _oid
        responses:
            400: 
                description: bad request
            200:
                description: OK, returns object
                type: application/json
                schema: LossConfig
    """
    data = request.get_json()
    loss_config = LossConfig(**data)
    session.add(loss_config)
    session.commit()
    return get_loss_config()


@api.post('/calculation/run')
@csrf.exempt
def post_calculation_run():
    # curl -X post http://localhost:5000/calculation/run --header "Content-Type: application/json" --data '{"shakemap":"model/shapefiles.zip"}'

    # get data from database
    loss_config = session.query(LossConfig).get(1)

    loss_model = session.query(LossModel).get(loss_config._lossmodel_oid)
    asset_collection = session.query(AssetCollection).get(
        loss_model._assetcollection_oid)

    vulnerability_model = session.query(VulnerabilityModel) \
        .join(LossModel, VulnerabilityModel.lossmodels). \
        filter(VulnerabilityModel.losscategory == loss_config.losscategory)\
        .first()

    # create in memory files
    # exposure.xml
    exposure_xml = create_exposure_xml(asset_collection)
    # exposure_assets.csv
    assets_csv = create_exposure_csv(asset_collection.assets)
    # vulnerability.xml
    vulnerability_xml = create_vulnerability_xml(vulnerability_model)
    # pre-calculation.ini
    hazard_ini = create_hazard_ini(loss_config)
    # risk.ini
    risk_ini = create_risk_ini(loss_config)

    # TODO: get shakemap
    shakemap_address = request.get_json()['shakemap']
    shakemap_zip = open(shakemap_address, 'rb')

    # send files to calculation endpoint
    files = {'job_config': hazard_ini,
             'input_model_1': exposure_xml,
             'input_model_2': assets_csv,
             'input_model_3': vulnerability_xml}

    response = requests.post(
        'http://localhost:8800/v1/calc/run', files=files)

    if response.ok:
        print("Upload completed successfully!")
        pre_job_id = response.json()['job_id']
    else:
        print("Something went wrong!")
        print(response.text)
        return make_response(jsonify({}), 200)

    # wait for pre-calculation to finish
    while requests.get(f'http://localhost:8800/v1/calc/{pre_job_id}/status')\
            .json()['status'] not in ['complete', 'failed']:
        time.sleep(1)
    response = requests.get(
        f'http://localhost:8800/v1/calc/{pre_job_id}/status')

    if response.json()['status'] != 'complete':
        return make_response(response.json(), 200)

    # send files to calculation endpoint
    files2 = {
        'job_config': risk_ini,
        'input_model_1': shakemap_zip
    }

    response = requests.post(
        'http://localhost:8800/v1/calc/run', files=files2,
        data={'hazard_job_id': pre_job_id})

    if response.ok:
        print("Upload completed successfully!")
    else:
        print("Something went wrong!")
        print(response.text)

    losscalculation = LossCalculation(
        shakemapid_resourceid='shakemap_address',
        _lossmodel_oid=loss_model._oid,
        losscategory=loss_config.losscategory,
        aggregateBy=loss_config.aggregateBy,
        timestamp_starttime=datetime.now()
    )
    session.add(losscalculation)
    session.commit()

    # wait, fetch and save results
    thread = threading.Thread(target=waitAndFetchResults(
        response.json()['job_id'], losscalculation._oid))
    thread.daemon = True
    thread.start()
    return make_response(response.json(), 200)


@api.get('/losscalculation')
@csrf.exempt
def get_loss_calculation():
    response = []
    loss_calculation = session.query(LossCalculation).all()

    for calculation in loss_calculation:
        calculation_dict = calculation._asdict()

        response.append(calculation_dict)

    return make_response(jsonify(response), 200)


def waitAndFetchResults(oqJobId, calcId):
    # wait for calculation to finish
    while requests.get(f'http://localhost:8800/v1/calc/{oqJobId}/status')\
            .json()['status'] not in ['complete', 'failed']:
        time.sleep(1)
    response = requests.get(
        f'http://localhost:8800/v1/calc/{oqJobId}/status')

    if response.json()['status'] != 'complete':
        return None
    # fetch results
    extractor = Extractor(oqJobId)
    data = extractor.get('avg_losses-rlzs').to_dframe()

    data = data[['asset_id', 'value']].rename(
        columns={'asset_id': '_asset_oid', 'value': 'loss_value'})

    # save results to database
    data = data.apply(lambda x: MeanAssetLoss(
        _losscalculation_oid=calcId, **x), axis=1)
    session.add_all(data)
    session.commit()
    print('Done saving results')
