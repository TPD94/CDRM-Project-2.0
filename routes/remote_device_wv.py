import os
from flask import Blueprint, jsonify, request, current_app, Response
import base64
from typing import Any, Optional, Union
from google.protobuf.message import DecodeError
from pywidevine.pssh import PSSH as widevinePSSH
from pywidevine import __version__
from pywidevine.cdm import Cdm as widevineCDM
from pywidevine.device import Device as widevineDevice
from pywidevine.exceptions import (InvalidContext, InvalidInitData, InvalidLicenseMessage, InvalidLicenseType,
                                   InvalidSession, SignatureMismatch, TooManySessions)

import yaml
from custom_functions.database.user_db import fetch_api_key, fetch_username_by_api_key
from custom_functions.user_checks.device_allowed import user_allowed_to_use_device
from pathlib import Path

remotecdm_wv_bp = Blueprint('remotecdm_wv', __name__)
with open(f'{os.getcwd()}/configs/config.yaml', 'r') as file:
    config = yaml.safe_load(file)

@remotecdm_wv_bp.route('/remotecdm/widevine', methods=['GET', 'HEAD'])
def remote_cdm_widevine():
    if request.method == 'GET':
        return jsonify({
            'status': 200,
            'message': f"{config['fqdn'].upper()} Remote Widevine CDM."
        })
    if request.method == 'HEAD':
        response = Response(status=200)
        response.headers['Server'] = f'https://github.com/devine-dl/pywidevine serve v{__version__}'
        return response

@remotecdm_wv_bp.route('/remotecdm/widevine/deviceinfo', methods=['GET'])
def remote_cdm_widevine_deviceinfo():
    if request.method == 'GET':
        base_name = config["default_wv_cdm"]
        if not base_name.endswith(".wvd"):
            base_name = (base_name + ".wvd")
        device = widevineDevice.load(f'{os.getcwd()}/configs/CDMs/WV/{base_name}')
        cdm = widevineCDM.from_device(device)
        return jsonify({
            'device_type': cdm.device_type.name,
            'system_id': cdm.system_id,
            'security_level': cdm.security_level,
            'host': f'{config["fqdn"]}/remotecdm/widevine',
            'secret': f'{config["remote_cdm_secret"]}',
            'device_name': Path(base_name).stem
        })

@remotecdm_wv_bp.route('/remotecdm/widevine/deviceinfo/<device>', methods=['GET'])
def remote_cdm_widevine_deviceinfo_specific(device):
    if request.method == 'GET':
        base_name = Path(device).with_suffix('.wvd').name
        api_key = request.headers['X-Secret-Key']
        username = fetch_username_by_api_key(api_key)
        device = widevineDevice.load(f'{os.getcwd()}/configs/CDMs/{username}/WV/{base_name}')
        cdm = widevineCDM.from_device(device)
        return jsonify({
            'device_type': cdm.device_type.name,
            'system_id': cdm.system_id,
            'security_level': cdm.security_level,
            'host': f'{config["fqdn"]}/remotecdm/widevine',
            'secret': f'{api_key}',
            'device_name': Path(base_name).stem
        })

@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/open', methods=['GET'])
def remote_cdm_widevine_open(device):
    if str(device).lower() == config['default_wv_cdm'].lower():
        wv_device = widevineDevice.load(f'{os.getcwd()}/configs/CDMs/WV/{config["default_wv_cdm"]}.wvd')
        cdm = current_app.config["CDM"] = widevineCDM.from_device(wv_device)
        session_id = cdm.open()
        return jsonify({
            'status': 200,
            'message': 'Success',
            'data': {
                'session_id': session_id.hex(),
                'device': {
                    'system_id': cdm.system_id,
                    'security_level': cdm.security_level,
                }
            }
        }), 200
    if request.headers['X-Secret-Key'] and str(device).lower() != config['default_wv_cdm'].lower():
        api_key = request.headers['X-Secret-Key']
        user = fetch_username_by_api_key(api_key=api_key)
        if user:
            if user_allowed_to_use_device(device=device, username=user):
                wv_device = widevineDevice.load(f'{os.getcwd()}/configs/CDMs/{user}/WV/{device}.wvd')
                cdm = current_app.config["CDM"] = widevineCDM.from_device(wv_device)
                session_id = cdm.open()
                return jsonify({
                    'status': 200,
                    'message': 'Success',
                    'data': {
                        'session_id': session_id.hex(),
                        'device': {
                            'system_id': cdm.system_id,
                            'security_level': cdm.security_level,
                        }
                    }
                }), 200
            else:
                return jsonify({
                    'message': f"Device '{device}' is not found or you are not authorized to use it.",
                    'status': 403
                }), 403
        else:
            return jsonify({
                'message': f"Device '{device}' is not found or you are not authorized to use it.",
                'status': 403
            }), 403
    else:
        return jsonify({
            'message': f"Device '{device}' is not found or you are not authorized to use it.",
            'status': 403
        }), 403


@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/close/<session_id>', methods=['GET'])
def remote_cdm_widevine_close(device, session_id):
        session_id = bytes.fromhex(session_id)
        cdm = current_app.config["CDM"]
        if not cdm:
            return jsonify({
                'status': 400,
                'message': f'No CDM for "{device}" has been opened yet. No session to close'
            }), 400
        try:
            cdm.close(session_id)
        except InvalidSession:
            return jsonify({
                'status': 400,
                'message': f'Invalid session ID "{session_id.hex()}", it may have expired'
            }), 400
        return jsonify({
            'status': 200,
            'message': f'Successfully closed Session "{session_id.hex()}".',
        }), 200

@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/set_service_certificate', methods=['POST'])
def remote_cdm_widevine_set_service_certificate(device):
    body = request.get_json()
    for required_field in ("session_id", "certificate"):
        if required_field == "certificate":
            has_field = required_field in body  # it needs the key, but can be empty/null
        else:
            has_field = body.get(required_field)
        if not has_field:
            return jsonify({
                'status': 400,
                'message': f'Missing required field "{required_field}" in JSON body'
            }), 400

    session_id = bytes.fromhex(body["session_id"])

    cdm = current_app.config["CDM"]
    if not cdm:
        return jsonify({
            'status': 400,
            'message': f'No CDM session for "{device}" has been opened yet. No session to use'
        }), 400

    certificate = body["certificate"]
    try:
        provider_id = cdm.set_service_certificate(session_id, certificate)
    except InvalidSession:
        return jsonify({
            'status': 400,
            'message': f'Invalid session id: "{session_id.hex()}", it may have expired'
        }), 400
    except DecodeError as error:
        return jsonify({
            'status': 400,
            'message': f'Invalid Service Certificate, {error}'
        }), 400
    except SignatureMismatch:
        return jsonify({
            'status': 400,
            'message': 'Signature Validation failed on the Service Certificate, rejecting'
        }), 400
    return jsonify({
        'status': 200,
        'message': f"Successfully {['set', 'unset'][not certificate]} the Service Certificate.",
        'data': {
            'provider_id': provider_id,
        }
    }), 200

@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/get_service_certificate', methods=['POST'])
def remote_cdm_widevine_get_service_certificate(device):
    body = request.get_json()
    for required_field in ("session_id",):
        if not body.get(required_field):
            return jsonify({
                'status': 400,
                'message': f'Missing required field "{required_field}" in JSON body'
            }), 400

    session_id = bytes.fromhex(body["session_id"])

    cdm = current_app.config["CDM"]

    if not cdm:
        return jsonify({
            'status': 400,
            'message': f'No CDM session for "{device}" has been opened yet. No session to use'
        }), 400

    try:
        service_certificate = cdm.get_service_certificate(session_id)
    except InvalidSession:
        return jsonify({
            'status': 400,
            'message': f'Invalid Session ID "{session_id.hex()}", it may have expired'
        }), 400
    if service_certificate:
        service_certificate_b64 = base64.b64encode(service_certificate.SerializeToString()).decode()
    else:
        service_certificate_b64 = None
    return jsonify({
        'status': 200,
        'message': 'Successfully got the Service Certificate',
        'data': {
            'service_certificate': service_certificate_b64,
        }
    }), 200

@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/get_license_challenge/<license_type>', methods=['POST'])
def remote_cdm_widevine_get_license_challenge(device, license_type):
    body = request.get_json()
    for required_field in ("session_id", "init_data"):
        if not body.get(required_field):
            return jsonify({
                'status': 400,
                'message': f'Missing required field "{required_field}" in JSON body'
            }), 400
    session_id = bytes.fromhex(body["session_id"])
    privacy_mode = body.get("privacy_mode", True)
    cdm = current_app.config["CDM"]
    if not cdm:
        return jsonify({
            'status': 400,
            'message': f'No CDM session for "{device}" has been opened yet. No session to use'
        }), 400
    if current_app.config.get("force_privacy_mode"):
        privacy_mode = True
        if not cdm.get_service_certificate(session_id):
            return jsonify({
                'status': 403,
                'message': 'No Service Certificate set but Privacy Mode is Enforced.'
            }), 403

    current_app.config['pssh'] = body['init_data']
    init_data = widevinePSSH(body['init_data'])

    try:
        license_request = cdm.get_license_challenge(
            session_id=session_id,
            pssh=init_data,
            license_type=license_type,
            privacy_mode=privacy_mode
        )
    except InvalidSession:
        return jsonify({
            'status': 400,
            'message': f'Invalid Session ID "{session_id.hex()}", it may have expired'
        }), 400
    except InvalidInitData as error:
        return jsonify({
            'status': 400,
            'message': f'Invalid Init Data, {error}'
        }), 400
    except InvalidLicenseType:
        return jsonify({
            'status': 400,
            'message': f'Invalid License Type {license_type}'
        }), 400
    return jsonify({
        'status': 200,
        'message': 'Success',
        'data': {
            'challenge_b64': base64.b64encode(license_request).decode()
        }
    }), 200


@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/parse_license', methods=['POST'])
def remote_cdm_widevine_parse_license(device):
    body = request.get_json()
    for required_field in ("session_id", "license_message"):
        if not body.get(required_field):
            return jsonify({
                'status': 400,
                'message': f'Missing required field "{required_field}" in JSON body'
            }), 400
    session_id = bytes.fromhex(body["session_id"])
    cdm = current_app.config["CDM"]
    if not cdm:
        return jsonify({
            'status': 400,
            'message': f'No CDM session for "{device}" has been opened yet. No session to use'
        }), 400
    try:
        cdm.parse_license(session_id, body['license_message'])
    except InvalidLicenseMessage as error:
        return jsonify({
            'status': 400,
            'message': f'Invalid License Message, {error}'
        }), 400
    except InvalidContext as error:
        return jsonify({
            'status': 400,
            'message': f'Invalid Context, {error}'
        }), 400
    except InvalidSession:
        return jsonify({
            'status': 400,
            'message': f'Invalid Session ID "{session_id.hex()}", it may have expired'
        }), 400
    except SignatureMismatch:
        return jsonify({
            'status': 400,
            'message': f'Signature Validation failed on the License Message, rejecting.'
        }), 400
    return jsonify({
        'status': 200,
        'message': 'Successfully parsed and loaded the Keys from the License message.',
    }), 200

@remotecdm_wv_bp.route('/remotecdm/widevine/<device>/get_keys/<key_type>', methods=['POST'])
def remote_cdm_widevine_get_keys(device, key_type):
    body = request.get_json()
    for required_field in ("session_id",):
        if not body.get(required_field):
            return jsonify({
                'status': 400,
                'message': f'Missing required field "{required_field}" in JSON body'
            }), 400
    session_id = bytes.fromhex(body["session_id"])
    key_type: Optional[str] = key_type
    if key_type == 'ALL':
        key_type = None
    cdm = current_app.config["CDM"]
    if not cdm:
        return jsonify({
            'status': 400,
            'message': f'No CDM session for "{device}" has been opened yet. No session to use'
        }), 400
    try:
        keys = cdm.get_keys(session_id, key_type)
    except InvalidSession:
        return jsonify({
            'status': 400,
            'message': f'Invalid Session ID "{session_id.hex()}", it may have expired'
        }), 400
    except ValueError as error:
        return jsonify({
            'status': 400,
            'message': f'The Key Type value "{key_type}" is invalid, {error}'
        }), 400
    keys_json = [
        {
            "key_id": key.kid.hex,
            "key": key.key.hex(),
            "type": key.type,
            "permissions": key.permissions
        }
        for key in keys
        if not key_type or key.type == key_type
    ]
    for entry in keys_json:
        if config['database_type'].lower() != 'mariadb':
            from custom_functions.database.cache_to_db_sqlite import cache_to_db
        elif config['database_type'].lower() == 'mariadb':
            from custom_functions.database.cache_to_db_mariadb import cache_to_db
        if entry['type'] != 'SIGNING':
            cache_to_db(pssh=str(current_app.config['pssh']), kid=entry['key_id'], key=entry['key'])

    return jsonify({
        'status': 200,
        'message': 'Success',
        'data': {
            'keys': keys_json
        }
    }), 200