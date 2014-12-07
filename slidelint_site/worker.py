"""
The workers module. Here is all staff related to worker, including sand-boxing,
slidelint checking and results storing.
"""
import base64
import subprocess
import operator
import os
import json
import itertools
import shutil
import zmq
from .utils import save_file
from shlex import split

import logging


HERE = os.path.dirname(os.path.abspath(__file__))

MESSAGE_FOR_SLIDELINT_EXCEPTION = \
    'Sorry, slidelint was not able to check your presentation...'
MESSAGE_FOR_TIMEOUT_EXCEPTION = \
    'Presentation checking take too long...'
MESSAGE_FOR_UNEXPECTED_EXCEPTION = \
    'Sorry, but our service has failed to check your presentation.'

DEFAULT_CONFIG_PATH = os.path.join(HERE, 'slidelint_config_files')

SLIDELINT_CONFIG_FILES_MAPPING = {
    'simple': os.path.join(DEFAULT_CONFIG_PATH, 'simple.cfg'),
    'strict': os.path.join(DEFAULT_CONFIG_PATH, 'strict.cfg')}


def remove_parrent_folder(file_path):
    """
    Cleanup function for worker - removes presentation parent folder.
    All data of new job are stored into directory (images, results, ...)
    so to cleanup we need to remove presentation parent directory.
    """
    parrent = file_path.rsplit(os.path.sep, 1)[0]
    shutil.rmtree(parrent)


def file_to_base64(path):
    """
    Read file data, encode it into base64. Return utf-8 string of base64
    encode data
    """
    return base64.b64encode(open(path, 'rb').read()).decode("utf-8")


def get_pdf_file_preview(path, size='300', timeout=600):
    """ Creates pdf file preview - small images of pdf file pages.
        For transformation pdf to bunch if images it uses pdftocairo, and
        store transformation results near the target pdf file.
        It returns:

    ::

        {'Slide N': base64_encoded_data, ...}

    """
    # making base name for preview images
    dist, file_name = path.rsplit(os.path.sep, 1)
    images_base_name = os.path.join(dist, 'preview-page')

    # transforming pdf file in preview images
    cmd = ['docker', 'run', '-t', '-v', dist + ':/presentation',
           '--networking=false', 'mithro/slidelint-box', 'pdftocairo',
           '/presentation/' + file_name, '/presentation/preview-page',
           '-jpeg', '-scale-to', size]

    retcode = subprocess.call(cmd, timeout=timeout)
    if retcode != 0:
        raise IOError("Can't create presentation preview")

    # collecting pages previews and decode its data into base64
    files = [i for i in os.listdir(dist) if i.startswith('preview-page')]
    files.sort()
    images = {'Slide %s' % (i+1): file_to_base64(os.path.join(dist, n))
              for i, n in enumerate(files)}
    # IDEA: do I need to add here images removing, or just left if for
    # global garbage collector?
    return images


def peform_slides_linting(presentation_path, config_path=None,
                          slidelint_cmd_pattern=None, timeout=1200):
    """
    function takes:
        * location to pdf file to check with slidelint
        * path to config file fro
        * path to slidelint executable
        * timeout for checking procedure

    function constructs command to lint presentation, executes it, and prepares
    results by grouping it by page:

    ::

         [{'page': page,
           'results': {"msg_name": ,
                       "msg": "",
                       "help": "",
                       "page":"Slide N",
                       "id":""}
            }, ...
         ]

    """
    if slidelint_cmd_pattern is None:
        slidelint_cmd_pattern = \
            'slidelint '\
            '-f json '\
            '--files-output={presentation_location}/{presentation_name}.res ' \
            '--config={config_path} '\
            '{presentation_location}/{presentation_name}'
    logging.debug("run slidelint check")
    results_path = presentation_path + '.res'  # where to store check results

    presentation_location, presentation_name = os.path.split(presentation_path)
    slidelint = slidelint_cmd_pattern.format(
        presentation_location=presentation_location,
        presentation_name=presentation_name,
        config_path=config_path,
        default_config_path=DEFAULT_CONFIG_PATH)
    # making command to execute
    cmd = split(slidelint)

    # Running slidelint checking against presentation.
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,)
    try:
        _, errs = process.communicate(timeout=timeout)  # pylint: disable=E1123
    except subprocess.TimeoutExpired:
        process.kill()
        # _, errs = process.communicate()  # this may cause hangs
        raise TimeoutError(
            "Program failed to finish within %s seconds!" % timeout)
    if process.returncode != 0:
        raise RuntimeError(
            "The command: '%s' died with the following traceback"
            ":\n\n%s" % (" ".join(cmd), errs))

    # getting checking result and grouping them by pages
    results = json.load(open(results_path, 'r'))
    results.sort(key=operator.itemgetter('page'))
    grouped_by_page = itertools.groupby(
        results, key=operator.itemgetter('page'))
    grouped_by_page = [{'page': page, 'results': list(result)}
                       for page, result in grouped_by_page]
    grouped_by_page.sort(key=operator.itemgetter('page'))

    return grouped_by_page


def store_for_debug(job, exp, debug_storage, debug_url):
    """
    Save problem presentation for debugging
    """
    msg = "Slidelint process died while trying to check presentation. You can"\
          " access this presentation by link %s. %s"
    file_object = open(job['file_path'], 'rb')
    save_file(file_object, job['uid'], debug_storage)
    link = "%s/%s/%s" % (debug_url, job['uid'], 'presentation.pdf')
    logging.error(msg, link, exp)


# pylint: disable=R0913,R0914
def worker(
        producer_chanel,
        collector_chanel,
        slidelint_cmd_pattern,
        debug_storage,
        debug_url,
        config_files_mapping,
        one_time_worker=False):
    """
    receives jobs and perform slides linting
    """
    context = zmq.Context()
    # recieve work
    consumer_receiver = context.socket(zmq.PULL)
    consumer_receiver.connect(producer_chanel)
    # send work
    consumer_sender = context.socket(zmq.PUSH)
    consumer_sender.connect(collector_chanel)
    logging.info("debug information will be stored into %s", debug_storage)
    logging.info("Worker is started on '%s' for receiving jobs"
                 " and on '%s' to send results",
                 producer_chanel, collector_chanel)
    while True:
        job = consumer_receiver.recv_json()
        logging.debug("new job with uid '%s'" % job['uid'])
        result = {'uid': job['uid'], 'status_code': 500}
        config_file = config_files_mapping.get(job['checking_rule'], None)
        try:
            try:
                # This try-except is for preforming linting second time in
                # case if first time it's failed
                slidelint_output = peform_slides_linting(
                    job['file_path'], config_file, slidelint_cmd_pattern)
            except:
                slidelint_output = peform_slides_linting(
                    job['file_path'], config_file, slidelint_cmd_pattern)
            icons = get_pdf_file_preview(job['file_path'])
            result['result'] = slidelint_output
            result['icons'] = icons
            result['status_code'] = 200
            logging.debug("successfully checked uid '%s'" % job['uid'])
        except RuntimeError as exp:
            store_for_debug(job, exp, debug_storage, debug_url)
            result['result'] = MESSAGE_FOR_SLIDELINT_EXCEPTION
        except TimeoutError as exp:
            store_for_debug(job, exp, debug_storage, debug_url)
            result['result'] = MESSAGE_FOR_TIMEOUT_EXCEPTION
        except BaseException as exp:
            store_for_debug(job, exp, debug_storage, debug_url)
            result['result'] = MESSAGE_FOR_UNEXPECTED_EXCEPTION
        finally:
            consumer_sender.send_json(result)
            logging.debug("cleanup for job with uid '%s'" % job['uid'])
            remove_parrent_folder(job['file_path'])
            if one_time_worker:
                logging.info("worker exiting")
                import sys
                sys.exit()


def worker_cli():
    """
    Worker for slidelint site

    Usage:
      slidelint_worker CONFIG [options]
      slidelint_worker -h | --help

    Arguments:
        CONFIG  Path to slidelint worker configuration file

    Options:
      -h --help           Show this screen.
      --producer=<addr>   ZMQ allowed address for jobs producer
      --collector=<addr>  ZMQ allowed address for results collector
      --slidelint=<path>  Path to slidelint executable
      --debug_storage=<path>  Directory to save errors debug information
      --onetime           Throw worker after it has completed a job
      --debug_url         Base url to debug information
    """
    import configparser
    from docopt import docopt
    import logging.config as _logging_config
    args = docopt(worker_cli.__doc__)
    _logging_config.fileConfig(args['CONFIG'])
    config = configparser.ConfigParser()
    config.read(args['CONFIG'])
    worker_config = config['slidelint_worker']
    producer = args['--producer'] or worker_config['producer']
    collector = args['--collector'] or worker_config['collector']
    slidelint = args['--slidelint'] or worker_config['slidelint']
    onetime = args['--onetime'] or worker_config['onetime']
    debug_storage = args['--debug_storage'] or worker_config['debug_storage']
    debug_url = args['--debug_url'] or worker_config['debug_url']
    config_files_mapping = 'config_files_mapping' in config \
        and dict(config['config_files_mapping']) \
        or SLIDELINT_CONFIG_FILES_MAPPING
    worker(collector, producer, slidelint, debug_storage, debug_url,
           config_files_mapping, onetime)
