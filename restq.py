import time
from collections import OrderedDict
from threading import Lock
from functools import wraps
import traceback
import json
import httplib

import bottle
from bottle import request


# The bytes data limit per job PUT
JOB_DATA_MAX_SIZE = 1024 * 2

# The number of seconds a job can be leased for before it can be handed out to
# a new requester
JOB_LEASE_TIME = 60 * 10 


# Need to iterator to circle (ring) around the ordered dict but enable a revert
# of the last yielded object #to ensure our queue does not loose its order/time
# consistency
class QueueIterator:
    def __init__(self, queue):
        self._queue = queue
        self._count = 0
        self._iter = self._queue.iteritems()

    def next(self):
        assert self._queue
        while True:
            try:
                obj = self._iter.next()
                self._count += 1
                break
            except StopIteration:
                self._iter = self._queue.iteritems()
        return obj

    def revert(self):
        self._count -= 1
        self._iter = self._queue.iteritems()
        index = len(self._queue) % self._count
        while index > 0:
            self._iter.next()
            index -= 1


# Serialise access to functions of Jobs
def serialise(func):
    @wraps(func)
    def with_serialisation(self, *a, **k):
        with self.lock:
            return func(self, *a, **k)
    return with_serialisation


JOB_DATA = 0
JOB_TASKS = 1
JOB_QUEUES = 2

class Jobs:
    def __init__(self):
        self.queues = {}
        self.queue_iter = {}
        self.tasks = {}
        self.jobs = {}
        self.lock = Lock()

    @serialise
    def remove(self, job_id):
        """remove job_id from the system"""
        #remove the job
        job = self.jobs.pop(job_id)
        
        #clean out the queues
        for queue_id in job[JOB_QUEUES]:
            queue = self.queues[queue_id]
            queue.pop(job_id)

        #clean out the tasks 
        for task_id in job[JOB_TASKS]:
            task = self.tasks[task_id]
            task.remove(job_id)
            if not task:
                self.tasks.pop(task_id)

    @serialise
    def add(self, job_id, task_id, queue_id, data):
        """store a job into a queue"""
        #store our job 
        job = self.jobs.get(job_id, None)
        if job is None:
            job = (data, set(), set())
            self.jobs[job_id] = job
        else:
            if data != job[JOB_DATA]:
                msg = "old job entry and new data != old data (%s)" % (job_id)
                raise ValueError(msg)

        job[JOB_TASKS].add(task_id)
        job[JOB_QUEUES].add(queue_id)

        #add this job to the queue
        queue = self.queues.get(queue_id, None)
        if queue is None:
            queue = OrderedDict()
            self.queues[queue_id] = queue
            self.queue_iter[queue_id] = QueueIterator(queue)
        if job_id not in queue:
            queue[job_id] = 0
       
        #add this job to the specified task
        task = self.tasks.get(task_id, None)
        if task is None:
            task = set()
            self.tasks[task_id] = task
        task.add(job_id)
        
    @serialise
    def pull(self, count):
        """pull out a max of count jobs"""
        queues_ids = self.queues.keys()
        queues_ids.sort()
        jobs = [] 
        for queue_id in self.queues:
            #skip queues that have no jobs
            if not self.queues[queue_id]:
                continue 
            
            #get our queue iterator
            iterator = self.queue_iter[queue_id]
            
            #pull a max of count jobs from the queue
            while True:
                job_id, dequeue_time = iterator.next()
                print job_id
                
                #add this job to the jobs result dict and update its lease time 
                ctime = time.time()
                if ctime - dequeue_time > JOB_LEASE_TIME:
                    self.queues[queue_id][job_id] = ctime
                    jobs.append((job_id, self.jobs[job_id][JOB_DATA]))
                    if len(jobs) >= count:
                        return jobs
                else:
                    #iff the queues maintain insert order and the iteration
                    #sequence walks the queue on that insert order we can 
                    #rely on the lease time being consistent such that all jobs
                    #following this point will also met this same condition
                    # -> go to next queue
                    iterator.revert()
                    break
        return jobs 

    @property
    def status(self):
        """return the status of the indexes"""
        queue_status = {}
        for key in self.queues:
            queue_status[key] = len(self.queues[key])
        return dict(total_jobs = len(self.jobs),
             total_tasks = len(self.tasks),
             queues = queue_status)

jobs = Jobs()


# Remove a job from a queue 
@bottle.delete('/job/<job_id>')
def job_delete(job_id):
    try:
        jobs.remove(job_id)
    except:
        bottle.abort(httplib.INTERNAL_SERVER_ERROR, traceback.format_exc())


# Put a job into a queue
@bottle.put('/job/<job_id>')
def job_put(job_id):
    """
    Required fields:
        task_id - input type='text' - defines which task the job belongs to
        queue_id - input type='int' - defines which queue_id the job belongs 
    Optional fields:
        data - input type='file' - data returned on GET job request
             - Max size data is JOB_DATA_MAX_SIZE
    """
    task_id = request.forms.get('task_id', type=str)
    queue_id = request.forms.get('queue_id', type=int)
    if task_id is None or queue_id is None:
        bottle.abort(httplib.BAD_REQUEST, 'Require task_id and queue_id')
    
    data = request.files.get('data')
    if data is not None:
        data = data.file.read(10000)

    try:
        jobs.add(job_id, task_id, queue_id, data)
    except:
        bottle.abort(httplib.INTERNAL_SERVER_ERROR, traceback.format_exc())


# Get the next job
@bottle.get('/job/')
def job_get():
    try:
        count = request.GET.get('count', default=1, type=int)
        job = jobs.pull(count=count)
    except:
        bottle.abort(httplib.INTERNAL_SERVER_ERROR, traceback.format_exc())
    return job

# Get the status 
@bottle.get('/status')
def status():
    try:
        status = jobs.status
    except:
        bottle.abort(httplib.INTERNAL_SERVER_ERROR, traceback.format_exc())
    return status

if __name__ == "__main__":
    bottle.run(host='localhost', port=8080, debug=True)

