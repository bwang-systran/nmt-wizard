import uuid
import os
import logging
import flask
import json

from six.moves import configparser

from nmtwizard import common, config, task
from nmtwizard.redis_database import RedisDatabase

ch = logging.StreamHandler()
ch.setLevel(logging.ERROR)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)

config.add_log_handler(ch)
common.add_log_handler(ch)

cfg = configparser.ConfigParser()
cfg.read('settings.ini')
MODE = os.getenv('LAUNCHER_MODE', 'Production')

redis_password = None
if cfg.has_option(MODE, 'redis_password'):
    redis_password = cfg.get(MODE, 'redis_password')

redis = RedisDatabase(cfg.get(MODE, 'redis_host'),
                      cfg.getint(MODE, 'redis_port'),
                      cfg.get(MODE, 'redis_db'),
                      redis_password)
services = config.load_services(cfg.get(MODE, 'config_dir'))


def _get_service(service):
    """Wrapper to fail on invalid service."""
    if service not in services:
        response = flask.jsonify(message="invalid service name: %s" % service)
        flask.abort(flask.make_response(response, 404))
    return services[service]


app = flask.Flask(__name__)

@app.route("/list_services", methods=["GET"])
def list_services():
    return flask.jsonify({k: services[k].display_name for k in services})

@app.route("/describe/<string:service>", methods=["GET"])
def describe(service):
    service_module = _get_service(service)
    return flask.jsonify(service_module.describe())

@app.route("/check/<string:service>", methods=["GET"])
def check(service):
    service_options = flask.request.get_json() if flask.request.is_json else None
    if service_options is None:
        service_options = {}
    service_module = _get_service(service)
    try:
        details = service_module.check(service_options)
    except ValueError as e:
        flask.abort(flask.make_response(flask.jsonify(message=str(e)), 400))
    except Exception as e:
        flask.abort(flask.make_response(flask.jsonify(message=str(e)), 500))
    else:
        return flask.jsonify(message=details)

@app.route("/launch/<string:service>", methods=["POST"])
def launch(service):
    content = None
    files = {}
    if flask.request.is_json:
        content = flask.request.get_json()
    else:
        content = flask.request.form.get('content')
        if content is not None:
            content = json.loads(content)
        for k in flask.request.files:
            files[k] = flask.request.files[k].read()
    if content is None:
        flask.abort(flask.make_response(flask.jsonify(message="missing content in request"), 400))
    service_module = _get_service(service)
    content["service"] = service
    task_id = str(uuid.uuid4())
    if 'trainer_id' in content and content['trainer_id']:
        task_id = (content['trainer_id']+'_'+task_id)[0:35]
    # Sanity check on content.
    if 'options' not in content or not isinstance(content['options'], dict):
        flask.abort(flask.make_response(flask.jsonify(message="invalid options field"), 400))
    if 'docker' not in content:
        flask.abort(flask.make_response(flask.jsonify(message="missing docker field"), 400))
    resource = service_module.get_resource_from_options(content["options"])
    task.create(redis, task_id, resource, service, content, files)
    return flask.jsonify(task_id)

@app.route("/status/<string:task_id>", methods=["GET"])
def status(task_id):
    if not task.exists(redis, task_id):
        flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
    response = task.info(redis, task_id, [])
    return flask.jsonify(response)

@app.route("/del/<string:task_id>", methods=["GET"])
def del_task(task_id):
    response = task.delete(redis, task_id)
    if isinstance(response, list) and not response[0]:
        flask.abort(flask.make_response(flask.jsonify(message=response[1]), 400))        
    return flask.jsonify(message="deleted %s" % task_id)

@app.route("/list_tasks/<string:pattern>", methods=["GET"])
def list_tasks(pattern):
    ltask = []
    for task_key in task.scan_iter(redis, pattern):
        task_id = task.id(task_key)
        info = task.info(redis, task_id, ["queued_time", "service", "content", "status", "message"])
        content = json.loads(info["content"])
        info["image"] = content['docker']['image']
        del info['content']
        info['task_id'] = task_id
        ltask.append(info)
    return flask.jsonify(ltask)

@app.route("/terminate/<string:task_id>", methods=["GET"])
def terminate(task_id):
    with redis.acquire_lock(task_id):
        current_status = task.info(redis, task_id, "status")
        if current_status is None:
            flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
        elif current_status == "stopped":
            return flask.jsonify(message="%s already stopped" % task_id)
        phase = flask.request.args.get('phase')
        task.terminate(redis, task_id, phase=phase)
    return flask.jsonify(message="terminating %s" % task_id)

@app.route("/beat/<string:task_id>", methods=["GET"])
def beat(task_id):
    duration = flask.request.args.get('duration')
    try:
        if duration is not None:
            duration = int(duration)
    except ValueError:
        flask.abort(flask.make_response(flask.jsonify(message="invalid duration value"), 400))
    container_id = flask.request.args.get('container_id')
    if not task.exists(redis, task_id):
        flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
    task.beat(redis, task_id, duration, container_id)
    return flask.jsonify(200)

@app.route("/log/<string:task_id>", methods=["GET"])
def get_log(task_id):
    if not task.exists(redis, task_id):
        flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
    content = task.get_log(redis, task_id)
    if content is None:
        flask.abort(flask.make_response(flask.jsonify(message="no logs for task %s" % task_id), 404))
    response = flask.make_response(content)
    response.mimetype = 'text/plain'
    return response

@app.route("/file/<string:task_id>/<string:filename>", methods=["GET"])
def get_file(task_id, filename):
    if not task.exists(redis, task_id):
        flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
    content = task.get_file(redis, task_id, filename)
    if content is None:
        flask.abort(flask.make_response(
            flask.jsonify(message="cannot find file %s for task %s" % (filename, task_id)), 404))
    response = flask.make_response(content)
    return response

@app.route("/file/<string:task_id>/<string:filename>", methods=["POST"])
def post_file(task_id, filename):
    if not task.exists(redis, task_id):
        flask.abort(flask.make_response(flask.jsonify(message="task %s unknown" % task_id), 404))
    content = flask.request.get_data()
    task.set_file(redis, task_id, content, filename)
    return flask.jsonify(200)
