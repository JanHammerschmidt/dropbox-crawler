import sys, os, shutil, signal, logging
import dropbox, msgpack
from datetime import datetime
from threading import Thread, Event
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata, FolderMetadata

db_token = '' # insert own db token here
db_path = '' # for dropbox root use ''. Otherwise prepend a '/'

log = logging.getLogger('crawler')
console = logging.StreamHandler()

finished = Event()
stop_request = False
finished_crawling = False
data_file = 'data.msgpack'
last_save = datetime.now()
save_interval = 120 # periodically save every n seconds

def exit_handler(signum, frame):
    global stop_request
    log.warn("waiting for crawler thread to finish")
    stop_request = True
    signal.signal(signal.SIGINT, original_sigint)
    try:
        if not finished.wait(60):
            log.error('thread timed out! data may be lost')
            sys.exit(1)
    except KeyboardInterrupt:
        print('exiting anyway? (data may be lost!)')
        sys.exit(1)
    sys.exit(0)

def update_tree(data):
    log.debug('new data (%i entries)' % len(data.entries))
    for e in data.entries:
        path_components = e.path_display[1:].split('/')
        folder = root
        for f in path_components[:-1]:
            try:
                folder = folder.folders[f]
            except KeyError:
                new_folder = Folder(f)
                folder.folders[f] = new_folder
                folder = new_folder
        f = path_components[-1]
        if isinstance(e, FileMetadata):
            folder.files[f] = File(f, e.size)
        elif isinstance(e, FolderMetadata):
            folder.folders[f] = Folder(f)
        else: #DeletedMetadata
            if f in folder.files:
                del folder.files[f]
            if f in folder.folders:
                del folder.folders[f]
    return data.cursor

def crawl():
    global space_used, space_allocated

    log.info('get space usage..')
    data = dbx.users_get_space_usage()
    space_used = data.used
    space_allocated = data.allocation.get_individual().allocated

    global crawl_cursor, finished_crawling
    if not finished_crawling:
        log.info('start crawling..')
        if crawl_cursor == None:
            data = dbx.files_list_folder(db_path, recursive=True)
            crawl_cursor = update_tree(data)
            save_data()
        while not stop_request:
            data = dbx.files_list_folder_continue(crawl_cursor)
            crawl_cursor = update_tree(data)
            save_data()
            if not data.has_more:
                log.info('no further data')
                finished_crawling = True
                save_data()
                break

    log.info('poll for changes..')
    global update_cursor
    while not stop_request:
        changes = dbx.files_list_folder_longpoll(update_cursor) # todo: backoff in data?
        if stop_request:
            break
        if changes.changes:
            data = dbx.files_list_folder_continue(update_cursor)
            update_cursor = update_tree(data)
            if (datetime.now() - last_save).total_seconds() > save_interval:
                save_data()

    finished.set()

class File:
    def __init__(self, name, size):
        self.name = name
        self.size = size

    def msgpack_pack(self):
        return msgpack.ExtType(81,
            msgpack.packb({'name': self.name, 'size': self.size}, use_bin_type=True))


class Folder:
    def __init__(self, name, files=[], folders=[]):
        self.name = name
        self.files = {f.name: f for f in files}
        self.folders = {f.name: f for f in folders}

    def msgpack_pack(self):
        return msgpack.ExtType(21,
            msgpack.packb({'name': self.name,
                           'files': list(self.files.values()),
                           'folders': list(self.folders.values())}, use_bin_type=True, default=lambda o: o.msgpack_pack()))

def msgpack_unpack(code, data):
    if code == 21:
        data = msgpack.unpackb(data, encoding='utf-8', ext_hook=msgpack_unpack)
        return Folder(data['name'], data['files'], data['folders'])
    elif code == 81:
        data = msgpack.unpackb(data, encoding='utf-8', ext_hook=msgpack_unpack)
        return File(data['name'], data['size'])
    raise RuntimeError('unknown msgpack extension type %i', code)

def load_data():
    global root, crawl_cursor, update_cursor, finished_crawling, space_used, space_allocated, last_save
    try:
        with open(data_file, 'rb') as f:
            data = msgpack.unpack(f, encoding='utf-8', ext_hook=msgpack_unpack)
        root = data['root']
        crawl_cursor = data['crawl_cursor']
        update_cursor = data['update_cursor']
        finished_crawling = data['finished_crawling']
        space_used = data['space_used']
        space_allocated = data['space_allocated']
        last_save = datetime.fromtimestamp(data['last_save'])
        log.info('successfully loaded data')
        return True
    except:
        log.info("loading data failed")
        root = Folder('root')
        crawl_cursor = None
        log.debug("getting update cursor")
        update_cursor = dbx.files_list_folder_get_latest_cursor(db_path, recursive=True, include_deleted=True).cursor
    return False

def save_data():
    global last_save
    log.debug('save data to %s' % data_file)
    was_finished = finished.is_set()
    finished.clear() # don't kill the process during saving data!
    try:
        shutil.move(data_file, 'data.prev.msgpack')
    except:
        pass
    last_save = datetime.now()
    data = {
        'root': root,
        'crawl_cursor': crawl_cursor,
        'update_cursor': update_cursor,
        'finished_crawling': finished_crawling,
        'space_used': space_used,
        'space_allocated': space_allocated,
        'last_save': last_save.timestamp()
    }
    with open(data_file, 'wb') as f:
        msgpack.pack(data, f, default=lambda o: o.msgpack_pack())
    if was_finished:
        finished.set()

def init_logging():
    formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(threadName)s: '
                                  '[%(name)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

if __name__ == '__main__':
    init_logging()
    log.info('connecting to dropbox')
    global dbx, original_sigint
    dbx = dropbox.Dropbox(db_token)

    # Check that the access token is valid
    try:
        dbx.users_get_current_account()
    except AuthError as err:
        sys.exit("ERROR: Invalid access token; try re-generating an access token from the app console on the web.")

    load_data()
    Thread(target=crawl).start()

    #print('polling for updates..')

    original_sigint = signal.signal(signal.SIGINT, exit_handler)
    signal.pause()

