import concurrent.futures
import os
import re
import datetime
import subprocess
import time
from collections import defaultdict
from functools import partial

from app import db, lock_app
from app.app_logger import logger
from app.domain import Rule, WordList, NONE_ENUM, ProgressLock
from app.hashcat_cmd import HashcatStatus, HashcatCmd
from app.nvidia_smi import set_cuda_visible_devices
from app.utils import split_uppercase, extract_essid_key, date_formatted
from app.uploader import UploadedTask


def subprocess_call(args):
    """
    Called in background process.
    :param args: shell args
    """
    logger.debug(">>> {}".format(' '.join(args)))
    process = subprocess.Popen(args,
                               universal_newlines=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, err = process.communicate()
    return out, err


def monitor_timer(func):
    def wrapped(*args, **kwargs):
        start = time.time()
        res = func(*args, **kwargs)
        elapsed_sec = time.time() - start
        timer = Attack.timers[func.__name__]
        timer['count'] += 1
        timer['elapsed'] += elapsed_sec
        return res
    return wrapped


class Attack(object):

    timers = defaultdict(lambda: dict(count=0, elapsed=1e-6))

    def __init__(self, uploaded_task: UploadedTask, timeout: int, status_timer: int):
        self.capture_path = uploaded_task.filename
        self.wordlist = None if uploaded_task.wordlist == NONE_ENUM else WordList(uploaded_task.wordlist)
        self.rule = None if uploaded_task.rule == NONE_ENUM else Rule(uploaded_task.rule)
        self.hashcat_status = HashcatStatus(timeout, status_timer)
        self.response = {
            'capture': self.capture_path,
            'status': "Running",
        }
        self.essid = None
        self.key_file = self.as_capture(".key")
        self.hcap_file = self.as_capture(".hccapx")
        self.new_cmd = partial(HashcatCmd, hcap_file=self.hcap_file, outfile=self.key_file)

    def as_capture(self, new_ext: str) -> str:
        """
        :param new_ext: new file extension path
        :return: capture filepath with the new extension
        """
        assert new_ext.startswith('.'), "Invalid new file extension"
        base = os.path.splitext(os.path.basename(self.capture_path))[0]
        new_file = os.path.join(os.path.dirname(self.capture_path), "{}{}".format(base, new_ext))
        return new_file

    @staticmethod
    def parse_essid(stdout: str):
        essid_key = "ESSID="
        for line in stdout.splitlines():
            if essid_key in line:
                start = line.index(essid_key) + len(essid_key)
                end = line.index(" (Length:", start)
                essid = line[start: end]
                return essid
        return None

    def is_already_cracked(self):
        return os.path.exists(self.key_file)

    def is_attack_needed(self):
        return os.path.exists(self.hcap_file) and not self.is_already_cracked()

    def get_key_status(self):
        key_password = None
        status = "Completed"
        if os.path.exists(self.key_file):
            with open(self.key_file, 'r') as f:
                key_password = extract_essid_key(f.read())
        elif not os.path.exists(self.hcap_file):
            status = "0 WPA handshakes captured"
        return key_password, status

    def cap2hccapx(self):
        """
        Convert airodump's `.cap` to hashcat's `.hccapx`
        """
        out, err = subprocess_call(['cap2hccapx', self.capture_path, self.hcap_file])
        self.essid = self.parse_essid(out)

    def run_essid_attack(self):
        """
        Run ESSID + digits_append.txt combinator attack.
        Run ESSID + best64.rule attack.
        """
        if self.essid is None:
            return
        if not self.is_attack_needed():
            return

        def modify_case(word):
            return {word, word.lower(), word.upper(), word.capitalize(), word.lower().capitalize()}

        essid_parts = {self.essid}
        regex_non_char = re.compile('[^a-zA-Z]')
        essid_parts.update(regex_non_char.split(self.essid))
        essid_parts.update(split_uppercase(self.essid))
        essids_case_insensitive = set()
        for essid in essid_parts:
            essid = regex_non_char.sub('', essid)
            essids_case_insensitive.update(modify_case(essid))
        essids_case_insensitive.update(modify_case(self.essid))
        essids_case_insensitive = filter(len, essids_case_insensitive)
        with open(WordList.ESSID.get_path(), 'w') as f:
            f.writelines([essid + '\n' for essid in essids_case_insensitive])
        self._run_essid_digits()
        self._run_essid_rule()

    @monitor_timer
    def _run_essid_digits(self):
        """
        Run ESSID + digits_append.txt combinator attack.
        """
        hashcat_cmd = self.new_cmd()
        hashcat_cmd.add_wordlist(WordList.ESSID)
        hashcat_cmd.add_wordlist(WordList.DIGITS_APPEND)
        hashcat_cmd.add_custom_argument("-a1")
        subprocess_call(hashcat_cmd.build())

    @monitor_timer
    def _run_essid_rule(self):
        """
        Run ESSID + best64.rule attack.
        """
        hashcat_cmd = self.new_cmd()
        hashcat_cmd.add_wordlist(WordList.ESSID)
        hashcat_cmd.add_rule(Rule.BEST_64)
        hashcat_cmd.pipe_word_candidates = True
        hashcat_cmd = ' '.join(hashcat_cmd.build())
        os.system(hashcat_cmd)

    @monitor_timer
    def run_digits8(self):
        """
        Run digits8+ attack. This includes:
        - birthdays 100 years backward
        - simple digits like 88888888, 12345678, etc.
        For more information refer to `digits/create_digits.py`
        """
        if not self.is_attack_needed():
            return
        hashcat_cmd = self.new_cmd()
        hashcat_cmd.add_wordlist(WordList.DIGITS_8)
        subprocess_call(hashcat_cmd.build())

    @monitor_timer
    def run_weak_passwords(self):
        """
        Run weak password attack, using a very shallow yet commonly used dictionaries:
        - john.txt
        - conficker.txt
        """
        if not self.is_attack_needed():
            return
        hashcat_cmd = self.new_cmd()
        hashcat_cmd.add_wordlist(WordList.WEAK)
        hashcat_cmd.add_rule(Rule.BEST_64)
        hashcat_cmd.pipe_word_candidates = True
        hashcat_cmd = ' '.join(hashcat_cmd.build())
        os.system(hashcat_cmd)

    @monitor_timer
    def run_main_wordlist(self):
        """
        Run main attack, specified by the user through the client app.
        """
        if self.wordlist is None or not self.is_attack_needed():
            return
        hashcat_cmd = self.new_cmd()
        hashcat_cmd.add_wordlist(self.wordlist)
        hashcat_cmd.add_rule(self.rule)
        yield from self.hashcat_status.run_with_status(hashcat_cmd)


def _crack_async(attack: Attack, lock: ProgressLock):
    """
    Called in background process.
    :param attack: hashcat attack to crack uploaded capture
    """
    attack.cap2hccapx()
    attack.run_essid_attack()
    attack.run_weak_passwords()
    attack.run_digits8()
    for progress in attack.run_main_wordlist():
        with lock:
            lock.progress = progress
    key, status = attack.get_key_status()
    with lock:
        lock.status = status
        lock.key = key
        lock.completed = True
    logger.info("Finished cracking {}".format(attack.capture_path))
    for name, timer in attack.timers.items():
        logger.debug("Timer {}: {:.2f} sec".format(name, timer['elapsed'] / timer['count']))


def _hashcat_benchmark_async(benchmark_filepath):
    """
    Called in background process.
    """
    set_cuda_visible_devices()
    out, err = subprocess_call(['hashcat', '-m2500', "-b", "--machine-readable", "--quiet"])
    pattern = re.compile("\d+:2500:.*:.*:\d+\.\d+:\d+")
    total_speed = 0
    for line in filter(pattern.fullmatch, out.splitlines()):
        device_speed = int(line.split(':')[-1])
        total_speed += device_speed
    if total_speed > 0:
        snapshot = "{date},{speed}\n".format(date=date_formatted(), speed=total_speed)
        with lock_app, open(benchmark_filepath, 'a') as f:
            f.write(snapshot)


class HashcatWorker(object):
    def __init__(self, app):
        """
        Called in main process.
        :param app: flask app
        """
        # we don't need more than 1 thread since hashcat utilizes all devices at once
        self.workers = 1
        self.app = app
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
        self.futures = []
        self.status_timer = self.app.config['HASHCAT_STATUS_TIMER']
        self.locks = {}
        self.task_id_by_job_id = {}
        self.benchmark()

    def callback_attack(self, future: concurrent.futures.Future):
        job_id = id(future)
        lock = self.locks[job_id]
        exception = future.exception()
        with lock:
            if exception is not None:
                logger.error(exception)
                lock.status = repr(exception)
            elif future.cancelled():
                lock.status = "Canceled"
            lock.completed = True
        task_id = self.task_id_by_job_id[job_id]
        task = UploadedTask.query.get(task_id)
        with lock:
            task.status = lock.status
            task.progress = lock.progress
            task.found_key = lock.key
        task.duration = datetime.datetime.now() - task.uploaded_time
        db.session.commit()

    def crack_capture(self, uploaded_task: UploadedTask, timeout: int):
        """
        Called in main process.
        Starts cracking .cap file in parallel process.
        :param uploaded_task: uploaded .cap file task
        :param timeout: brute force timeout in minutes
        """
        lock = ProgressLock()
        attack = Attack(uploaded_task, timeout=timeout, status_timer=self.status_timer)
        future = self.executor.submit(_crack_async, attack=attack, lock=lock)
        job_id = id(future)
        self.locks[job_id] = lock
        self.task_id_by_job_id[job_id] = uploaded_task.id
        future.add_done_callback(self.callback_attack)
        self.futures.append(future)

    def benchmark(self):
        """
        Run hashcat WPA benchmark.
        """
        future = self.executor.submit(_hashcat_benchmark_async, benchmark_filepath=self.app.config['BENCHMARK_FILE'])
        self.futures.append(future)

    def terminate(self):
        futures_active = iter(future for future in self.futures if not future.done())
        for future in futures_active:
            future.cancel()
        subprocess_call(["pkill", "hashcat"])

    def __del__(self):
        self.executor.shutdown(wait=False)
