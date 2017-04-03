
# Pull in Python 3 string object on Python 2.
from builtins import str

import select, socket, struct, sys
import os, os.path
import logging, time, threading, math
import zmq

from collections import deque
from queue import Queue, Empty

from pymoku import Moku, FrameTimeout, BufferTimeout, NotDeployedException, InvalidOperationException, NoDataException, StreamException, InsufficientSpace, MPNotMounted, MPReadOnly, dataparser

from . import _instrument

log = logging.getLogger(__name__)

DL_STATE_NONE		= 0
DL_STATE_RUNNING 	= 1
DL_STATE_WAITING 	= 2
DL_STATE_INVAL		= 3
DL_STATE_FSFULL		= 4
DL_STATE_OVERFLOW	= 5
DL_STATE_BUSY		= 6
DL_STATE_STOPPED	= 7

class FrameQueue(Queue):
	def put(self, item, block=True, timeout=None):
		# Behaves the same way as default except that instead of raising Full, it
		# just pushes the item on to the deque anyway, throwing away old frames.
		self.not_full.acquire()
		try:
			if self.maxsize > 0 and block:
				if timeout is None:
					while self._qsize() == self.maxsize:
						self.not_full.wait()
				elif timeout < 0:
					raise ValueError("'timeout' must be a non-negative number")
				else:
					endtime = _time() + timeout
					while self._qsize() == self.maxsize:
						remaining = endtime - _time()
						if remaining <= 0.0:
							break
						self.not_full.wait(remaining)
			self._put(item)
			self.unfinished_tasks += 1
			self.not_empty.notify()
		finally:
			self.not_full.release()

	def get(self, block=True, timeout=None):
		item = None
		while True:
			try:
				item = Queue.get(self, block=block, timeout=timeout or 1)
			except Empty:
				if timeout is None:
					continue
				else:
					raise
			else:
				return item

	# The default _init for a Queue doesn't actually bound the deque, relying on the
	# put function to bound.
	def _init(self, maxsize):
		self.queue = deque(maxlen=maxsize)

class DataBuffer(object):
	"""
	Holds data from the internal buffer (prior to rendering)
	"""

	def __init__(self, ch1, ch2, xs, stateid, scales):
		self.ch1 = ch1
		self.ch2 = ch2
		self.xs = xs
		self.stateid = stateid
		self.scales = scales

class DataFrame(object):
	"""
	Superclass representing a full frame of some kind of data. This class is never used directly,
	but rather it is subclassed depending on the type of data contained and the instrument from
	which it originated. For example, the :any:`Oscilloscope` instrument will generate :any:`VoltsFrame`
	objects, where :any:`VoltsFrame` is a subclass of :any:`DataFrame`.
	"""
	def __init__(self):
		self.complete = False
		self.chs_valid = [False, False]

		#: Channel 1 raw data array. Present whether or not the channel is enabled, but the contents
		#: are undefined in the latter case.
		self.raw1 = []

		#: Channel 2 raw data array.
		self.raw2 = []

		self.stateid = None
		self.trigstate = None

		#: Frame number. Increments monotonically but wraps at 16-bits.
		self.frameid = 0

		#: Incremented once per trigger event. Wraps at 32-bits.
		self.waveformid = 0

		self.flags = None

	def add_packet(self, packet):
		hdr_len = 15
		if len(packet) <= hdr_len:
			# Should be a higher priority but actually seems unexpectedly common. Revisit.
			log.debug("Corrupt frame recevied, len %d", len(packet))
			return

		data = struct.unpack('<BHBBBBBIBH', packet[:hdr_len])
		frameid = data[1]
		instrid = data[2]
		chan = (data[3] >> 4) & 0x0F

		self.stateid = data[4]
		self.trigstate = data[5]
		self.flags = data[6]
		self.waveformid = data[7]
		self.source_serial = data[8]

		if self.frameid != frameid:
			self.frameid = frameid
			self.chs_valid = [False, False]

		log.debug("AP ch %d, f %d, w %d", chan, frameid, self.waveformid)

		# For historical reasons the data length is 1026 while there are only 1024
		# valid samples. Trim the fat.
		if chan == 0:
			self.chs_valid[0] = True
			self.raw1 = packet[hdr_len:-8]
		else:
			self.chs_valid[1] = True
			self.raw2 = packet[hdr_len:-8]

		self.complete = all(self.chs_valid)

		if self.complete:
			if not self.process_complete():
				self.complete = False
				self.chs_valid = [False, False]

	def process_complete(self):
		# Designed to be overridden by subclasses needing to transform the raw data in to Volts etc.
		return True


# Revisit: Should this be a Mixin? Are there more instrument classifications of this type, recording ability, for example?
class FrameBasedInstrument(_instrument.MokuInstrument):
	def __init__(self):
		super(FrameBasedInstrument, self).__init__()
		self._buflen = 1
		self._queue = FrameQueue(maxsize=self._buflen)
		self._hb_forced = False
		self._dlserial = 0
		self._dlskt = None
		self._dlftype = None
		self.logfile = None

		self.binstr = ''
		self.procstr = ''
		self.fmtstr = ''
		self.hdrstr = ''

		self.upload_index = {}

		self._strparser = None

	def set_frame_class(self, frame_class, **frame_kwargs):
		self.frame_class = frame_class
		self.frame_kwargs = frame_kwargs

	def flush(self):
		""" Clear the Frame Buffer.
		This is normally not required as one can simply wait for the correctly-generated frames to propagate through
		using the appropriate arguments to :any:`get_frame`.
		"""
		with self._queue.mutex:
			self._queue.queue.clear()

	def set_buffer_length(self, buflen):
		""" Set the internal frame buffer length."""
		self._buflen = buflen
		self._queue = FrameQueue(maxsize=buflen)

	def get_buffer_length(self):
		""" Return the current length of the internal frame buffer
		"""
		return self._buflen

	def get_frame(self, timeout=None, wait=True):
		""" Get a :any:`DataFrame` from the internal frame buffer
		"""
		try:
			# Dodgy hack, infinite timeout gets translated in to just an exceedingly long one
			endtime = time.time() + (timeout or sys.maxsize)
			while self._running:
				frame = self._queue.get(block=True, timeout=timeout)
				# Should really just wait for the new stateid to propagte through, but
				# at the moment we don't support stateid and stateid_alt being different;
				# i.e. we can't rerender already aquired data. Until we fix this, wait
				# for a trigger to propagate through so we don't at least render garbage
				if not wait or frame.trigstate == self._stateid:
					return frame
				elif time.time() > endtime:
					raise FrameTimeout()
				else:
					log.debug("Incorrect state received: %d/%d", frame.trigstate, self._stateid)
		except Empty:
			raise FrameTimeout()

	def get_buffer(self, timeout=None):
		""" Get a :any:`DataBuffer` from the internal data channel buffer.
		This will commit any outstanding device settings and pause acquisition.
		"""

		# Force a pause even if it already has happened
		self.set_pause(True)
		self.commit()
				
		# Get buffer data using a network stream
		self.datalogger_start_single(filetype='net')
		ch1 = []
		ch2 = []

		try:
			while True:
				ch, idx, data = self.datalogger_get_samples()
				if ch == 1:
					ch1 += data
				elif ch == 2:
					ch2 += data

		except NoDataException as e:
			self.datalogger_stop()

		# Get a frame to see what the acquisition state was for the current buffer
		# TODO: Need a way of getting buffer state information without frames
		try:
			frame = self.get_frame(timeout=timeout, wait=False)
		except FrameTimeout:
			raise BufferTimeout('Unable to retrieve buffer acquisition state')

		_buff = DataBuffer(ch1=ch1, ch2=ch2, xs=None, stateid=frame.trigstate, scales=None)

		# Allow children to post-process the buffer first
		return self._process_buffer(_buff)

	def _process_buffer(self, buff):
		# Expected to be overwritten by child class in the case of 
		# post-processing a buffer object
		return buff

	def _dlsub_init(self, tag):
		ctx = zmq.Context.instance()
		self._dlskt = ctx.socket(zmq.SUB)
		self._dlskt.connect("tcp://%s:27186" % self._moku._ip)
		self._dlskt.setsockopt_string(zmq.SUBSCRIBE, str(tag))

		self._strparser = dataparser.LIDataParser(self.ch1, self.ch2,
			self.binstr, self.procstr, self.fmtstr, self.hdrstr,
			self.timestep, int(time.time()), [0] * self.nch,
			0) # Zero offset from start time to first sample, valid for streams but not so much for single frame transfers


	def _dlsub_destroy(self):
		if self._dlskt is not None:
			self._dlskt.close()
			self._dlskt = None

	@staticmethod
	def _max_stream_rates(instr, nch, use_sd):
		"""
		Returns the maximum rate at which the instrument can be streamed for the given
		streaming configuration

		Currently only specified for the Oscilloscope instrument
		"""

		# These are checked on the client side too but sanity-check here as an invalid
		# rate can hard-hang the Moku. These rates are approximate and experimentally
		# derived, should be updated as we test and optimize things.
		# Logging rates depend on which storage medium, and the filetype as well
		maxrates = None
		if nch == 2:
			if(use_sd):
				maxrates = { 'bin' : 150e3, 'csv' : 1e3, 'net' : 20e3, 'plot' : 10}
			else:
				maxrates = { 'bin' : 1e6, 'csv' : 1e3, 'net' : 20e3, 'plot' : 10}
		else:
			if(use_sd):
				maxrates = { 'bin' : 250e3, 'csv' : 3e3, 'net' : 40e3, 'plot' : 10}
			else:
				maxrates = { 'bin' : 1e6, 'csv' : 3e3, 'net' : 40e3, 'plot' : 10}

		return maxrates

	
	def _estimate_logsize(self, ch1, ch2, duration, timestep, filetype):
		"""
		Returns a rough estimate of log size for disk space checking. 
		Currently assumes instrument is the Oscilloscope.
		"""
		if filetype is 'bin':
			sample_size_bytes = 4 * (ch1 + ch2)
			return (duration / timestep) * sample_size_bytes
		elif filetype is 'csv':
			# one byte per character: time, data (assume negative half the time), newline
			characters_per_line = 16 + ( 2 + 16.5 )*(ch1 + ch2) + 2
			return (duration / timestep) *  characters_per_line
	

	def datalogger_start(self, start=0, duration=10, use_sd=True, ch1=True, ch2=True, filetype='csv'):
		""" Start recording data with the current settings.

		Device must be in ROLL mode (via a call to :any:`set_xmode`) and the sample rate must be appropriate
		to the file type (see below).

		:raises InvalidOperationException: if the sample rate is too high for the selected filetype or if the
			device *x_mode* isn't set to *ROLL*.


		.. warning:: Start parameter not currently implemented, must be set to zero

		:param start: Start time in seconds from the time of function call
		:param duration: Log duration in seconds
		:type use_sd: bool
		:param use_sd: Log to SD card (default is internal volatile storage)
		:type ch1: bool
		:param ch1: Log from Channel 1
		:type ch2: bool
		:param ch2: Log from Channel 2
		:param filetype: Type of log to start. One of the types below.

		*File Types*

		- **csv** -- CSV file, 1kS/s max rate
		- **bin** -- LI Binary file, 10kS/s max rate
		- **net** -- Log to network, retrieve data with :any:`datalogger_get_samples`. 100smps max rate
		"""
		if not (bool(ch1) or bool(ch2)):
			raise InvalidOperationException("No channels were selected for logging")
		if duration <= 0:
			raise InvalidOperationException("Invalid duration %d", duration)
		
		from datetime import datetime
		if self._moku is None: raise NotDeployedException()
		# TODO: rest of the options, handle errors
		self._dlserial += 1

		self.tag = "%04d" % self._dlserial

		self.ch1 = bool(ch1)
		self.ch2 = bool(ch2)		
		self.nch = bool(self.ch1) + bool(self.ch2)

		fname = datetime.now().strftime(self.logname + "_%Y%m%d_%H%M%S")

		# Currently the data stream genesis is from the x_mode commit below, meaning that delayed start
		# doesn't work properly. Once this is fixed in the FPGA/daemon, remove this check and the note
		# in the documentation above.
		if start:
			raise InvalidOperationException("Logging start time parameter currently not supported")

		# Logging rates depend on which storage medium, and the filetype as well
		maxrates = FrameBasedInstrument._max_stream_rates(None, self.nch, use_sd)
		if math.floor(1.0 / self.timestep) > maxrates[filetype]:
			raise InvalidOperationException("Sample Rate %d too high for file type %s. Maximum rate: %d" % (1.0 / self.timestep, filetype, maxrates[filetype]))

		if self.x_mode != _instrument.ROLL:
			raise InvalidOperationException("Instrument must be in roll mode to perform data logging")

		if not all([ len(s) for s in [self.binstr, self.procstr, self.fmtstr, self.hdrstr]]):
			raise InvalidOperationException("Instrument currently doesn't support data logging")

		# Check mount point here
		mp = 'e' if use_sd else 'i'
		try:
			t , f = self._moku._fs_free(mp)
			logsize = self._estimate_logsize(ch1, ch2, duration, self.timestep, filetype)
			if f < logsize:
				raise InsufficientSpace("Insufficient disk space for requested log file (require %d kB, available %d kB)" % (logsize/(2**10), f/(2**10)))
		except MPReadOnly as e:
			if use_sd:
				raise MPReadOnly("SD Card is read only.")
			raise e
		except MPNotMounted as e:
			if use_sd:
				raise MPNotMounted("SD Card is unmounted.")
			raise e

		# We have to be in this mode anyway because of the above check, but rewriting this register and committing
		# is necessary in order to reset the channel buffers on the device and flush them of old data.
		self.x_mode = _instrument.ROLL
		self.commit()

		try:
			self._moku._stream_prep(ch1=ch1, ch2=ch2, start=start, end=start + duration, offset=0, timestep=self.timestep,
			binstr=self.binstr, procstr=self.procstr, fmtstr=self.fmtstr, hdrstr=self.hdrstr,
			fname=fname, ftype=filetype, tag=self.tag, use_sd=use_sd)
		except StreamException as e:
			self.datalogger_error(status=e.err)
		
		if filetype == 'net':
			self._dlsub_init(self.tag)

		self._moku._stream_start()

		# This may not actually exist as a file (e.g. if a 'net' session was run)
		self.logfile = str(self.datalogger_status()[4]).strip()

		# Store the requested filetype in the case of a "wait" call
		self._dlftype = filetype

	def datalogger_start_single(self, use_sd=False, ch1=True, ch2=True, filetype='csv'):
		""" Grab all currently-recorded data at full rate.

		Unlike a normal datalogger session, this will log only the data that has *already* been aquired through
		normal activities. For example, if the Oscilloscope has aquired a frame and is paused, this function will
		retrieve the data in that frame at the full underlying sample rate.

		:type use_sd: bool
		:param use_sd: Log to SD card (default is internal volatile storage)
		:type ch1: bool
		:param ch1: Log from Channel 1
		:type ch2: bool
		:param ch2: Log from Channel 2
		:param filetype: Type of log to start. One of the types below.

		*File Types*

		- **csv** -- CSV file, 1kS/s max rate
		- **bin** -- LI Binary file, 10kS/s max rate
		- **net** -- Log to network, retrieve data with :any:`datalogger_get_samples`. 100smps max rate
		"""
		if not (bool(ch1) or bool(ch2)):
			raise InvalidOperationException("No channels were selected for logging")
			
		from datetime import datetime
		if self._moku is None: raise NotDeployedException()
		# TODO: rest of the options, handle errors
		self._dlserial += 1

		self.tag = "%04d" % self._dlserial

		self.ch1 = bool(ch1)
		self.ch2 = bool(ch2)		
		self.nch = int(self.ch1) + int(self.ch2)

		# Determine what the log file name will be (if any)
		fname = datetime.now().strftime(self.logname + "_%Y%m%d_%H%M%S")

		if not all([ len(s) for s in [self.binstr, self.procstr, self.fmtstr, self.hdrstr]]):
			raise InvalidOperationException("Instrument currently doesn't support data logging")

		# Check mount point here
		mp = 'e' if use_sd else 'i'
		try:
			t , f = self._moku._fs_free(mp)
			# Fake a "duration" for 16k samples
			logsize = self._estimate_logsize(ch1, ch2, (2**14) * self.timestep , self.timestep, filetype)
			if f < logsize:
				raise InsufficientSpace("Insufficient disk space for requested log file (require %d kB, available %d kB)" % (logsize/(2**10), f/(2**10)))
		except MPReadOnly as e:
			if use_sd:
				raise MPReadOnly("SD Card is read only.")
			raise e
		except MPNotMounted as e:
			if use_sd:
				raise MPNotMounted("SD Card is unmounted.")
			raise e

		#TODO: Work out the offset from current span (instrument dependent?)
		try:
			self._moku._stream_prep(ch1=ch1, ch2=ch2, start=0, end=0, timestep=self.timestep, offset=0,
			binstr=self.binstr, procstr=self.procstr, fmtstr=self.fmtstr, hdrstr=self.hdrstr,
			fname=fname, ftype=filetype, tag=self.tag, use_sd=use_sd)
		except StreamException as e:
			self.datalogger_error(status=e.err)

		if filetype == 'net':
			self._dlsub_init(self.tag)

		self._moku._stream_start()

		self.logfile = str(self.datalogger_status()[4]).strip()

		# Store the requested filetype in the case of a "wait" call
		self._dlftype = filetype

	def datalogger_wait(self, timeout=None, upload=False):
		"""
		Handles the current datalogging session. 

		:type timeout: float
		:param timeout: Timeout period

		:type upload: bool
		:param upload: Upload log file to local directory when complete (ignored if `net` stream)

		:rtype: dict
		:return: If `net` stream was run, returns a dictionary containing `ch1` and `ch2` streamed data. Else `None`.

		:raises Streamxception:
		:raises InvalidOperationException: 
		:raises FrameTimeout: Timed out waiting for samples

		"""
		if self._dlftype is 'net':
			return self.datalogger_wait_net(timeout=timeout)
		elif self._dlftype in ['csv','bin']:
			self.datalogger_wait_file(timeout=timeout, upload=upload)
			return None
		else:
			raise InvalidOperationException('No valid datalogging session has been run')


	def datalogger_wait_file(self, timeout=None, upload=False):
		""" 
		Handles the current `csv` or `bin` datalogging session.

		:type timeout: float
		:param timeout: Timeout period

		:type upload: bool
		:param upload: Upload log file to local directory when complete

		:raises Streamxception:
		:raises InvalidOperationException: 
		:raises FrameTimeout: Timed out waiting for samples
		"""
		if self._dlftype in ['csv', 'bin']:
			while not self.datalogger_completed():
				self.datalogger_error()
				time.sleep(0.5)
			if upload:
				self.datalogger_upload()
		elif self._dlftype is None:
			raise InvalidOperationException('No datalogging session has been run')
		else:
			raise InvalidOperationException('Datalogging session run with invalid filetype {csv,bin}: %s' % self._dlftype)

	def datalogger_wait_net(self, timeout=None):
		""" 
		Handles the current datalogging network stream and collates streamed channel data.

		:type timeout: float
		:param timeout: Timeout period

		:rtype: dict
		:return: Dictionary containing `ch1` and `ch2` streamed data.

		:raises Streamxception:
		:raises InvalidOperationException: 
		:raises FrameTimeout: Timed out waiting for samples
		"""
		if self._dlftype is None:
			raise InvalidOperationException('No datalogging session has been run')
		elif self._dlftype == 'net':
			try:
				ch1 = []
				ch2 = []
				while True:
					self.datalogger_error()
					ch, idx, samples = self.datalogger_get_samples(timeout=timeout)
					if ch == 1:
						ch1 += samples
					if ch == 2:
						ch2 += samples
			except NoDataException:
				return {"ch1":ch1, "ch2":ch2}
		else:
			raise InvalidOperationException('Datalogging session is not a network stream: %s' % self._dlftype)

	def datalogger_stop(self):
		""" Stop a recording session previously started with :py:func:`datalogger_start`

		This function signals that the user no longer needs to know the status of the previous
		log, discarding that state. It must be called before a second log is started or else
		that start attempt will fail with a "busy" error.

		:rtype: int
		:return: final status code (see :py:func:`datalogger_status`
		"""
		if self._moku is None: raise NotDeployedException()

		stat = self._moku._stream_stop()
		self._dlsub_destroy()

		return stat

	def datalogger_status(self):
		""" Return the status of the most recent recording session to be started.
		This is still valid after the stream has stopped, in which case the status will reflect that it's safe
		to start a new session.

		Returns a tuple of state variables:

		- **status** -- Current datalogger state
		- **logged** -- Number of samples recorded so far. If more than one channel is active, this is the sum of all points across all channels.
		- **to start** -- Number of seconds until/since start. Time until start is positive, a negative number indicates that the record has started already.
		- **to end** -- Number of seconds until/since end.
		- **filename** -- Base filename of current log session (without filetype)

		Status is one of:

		- **DL_STATE_NONE** -- No session
		- **DL_STATE_RUNNING** -- Session currently running
		- **DL_STATE_WAITING** -- Session waiting to run (delayed start)
		- **DL_STATE_INVAL** -- An attempt was made to start a session with invalid parameters
		- **DL_STATE_FSFULL** -- A session has terminated early due to the storage filling up
		- **DL_STATE_OVERFLOW** -- A session has terminated early due to the sample rate being too high for the storage speed
		- **DL_STATE_BUSY** -- An attempt was made to start a session when one was already running
		- **DL_STATE_STOPPED** -- A session has successfully completed.

		:rtype: int, int, int, int
		:return: status, logged, to start, to end.
		"""
		if self._moku is None: raise NotDeployedException()
		return self._moku._stream_status()

	def datalogger_remaining(self):
		""" Returns number of seconds from session start and end.

		- **to start** -- Number of seconds until/since start. Time until start is positive, a negative number indicates that the record has started already.
		- **to end** -- Number of seconds until/since end.

		:rtype: int, int
		:return: to start, to end
		"""
		d1, d2, start, end, fname = self.datalogger_status()
		return start, end

	def datalogger_samples(self):
		""" Returns number of samples captures in this datalogging session.

		:rtype: int
		:returns: sample count
		"""
		return self.datalogger_status()[1]

	def datalogger_busy(self):
		""" Returns the readiness of the datalogger to start a new session.

		The data logger must not be busy before issuing a :any:`datalogger_start`, otherwise
		an exception will be raised.

		If the datalogger is busy, the time remaining may be queried to see how long it might be
		until it has finished what it's doing, or it can be forcibly stopped with a call to
		:any:`datalogger_stop`.

		:rtype: bool
		:returns: Whether or not a new session can be started.
		"""
		return self.datalogger_status()[0] != DL_STATE_NONE

	def datalogger_completed(self):
		""" Returns whether or not the datalogger is expecting to log any more data.

		If the log is completed then the results files are ready to be uploaded or simply
		read off the SD card. At most one subsequent :any:`datalogger_get_samples` call
		will return without timeout.

		If the datalogger has entered an error state, a StreamException is raised.

		:rtype: bool
		:returns: Whether the current session has finished running. 

		:raises StreamException: if the session has entered an error state
		"""
		status = self.datalogger_status()[0]
		self.datalogger_error(status=status)
		return status not in [DL_STATE_RUNNING, DL_STATE_WAITING]

	def datalogger_filename(self):
		""" Returns the current base filename of the logging session.

		The base filename doesn't include the file extension as multiple files might be
		recorded simultaneously with different extensions.

		:rtype: str
		:returns: The file name of the current, or most recent, log file.
		"""
		if self.logfile:
			return self.logfile.split(':')[1]
		else:
			return None

	def datalogger_error(self, status=None):
		""" Checks the current datalogger session for errors. Alternatively, the status
		parameter returned by :any:`datalogger_status` call can be translated to the 
		associated exception (if any).

		:raises StreamException: if the session is in error.
		:raises InvalidArgument:
		"""
		if not status:
			status = self.datalogger_status()[0]
		msg = None

		if status in [DL_STATE_NONE, DL_STATE_RUNNING, DL_STATE_WAITING, DL_STATE_STOPPED]:
			msg = None
		elif status == DL_STATE_INVAL:
			msg = "Invalid Parameters for Datalogger Operation"
		elif status == DL_STATE_FSFULL:
			msg = "Target Filesystem Full"
		elif status == DL_STATE_OVERFLOW:
			msg ="Session overflowed, sample rate too fast."
		elif status == DL_STATE_BUSY:
			msg = "Tried to start a logging session while one was already running."
		else:
			raise ValueError('Invalid status argument')

		if msg:
			raise StreamException(msg, status)


	def datalogger_upload(self):
		""" Load most recently recorded data files from the Moku to the local PC.

		:raises NotDeployedException: if the instrument is not yet operational.
		:raises InvalidOperationException: if no files are present.
		"""
		import re

		if self._moku is None: raise NotDeployedException()

		uploaded = 0
		target = self.datalogger_filename()

		if not target:
			raise InvalidOperationException("No data has been logged in current session.")
		# Check internal and external storage
		for mp in ['i', 'e']:
			try:
				for f in self._moku._fs_list(mp):
					if str(f[0]).startswith(target):
						# Don't overwrite existing files of the name name. This would be nicer
						# if we could pass receive_file a local filename to save to, but until
						# that change is made, just move the clashing file out of the way.
						if os.path.exists(f[0]):
							i = 1
							while os.path.exists(f[0] + ("-%d" % i)):
								i += 1

							os.rename(f[0], f[0] + ("-%d" % i))

						# Data length of zero uploads the whole file
						self._moku._receive_file(mp, f[0], 0)
						log.debug('Uploaded file %s',f[0])
						uploaded += 1
			except MPNotMounted:
				log.debug("Attempted to list files on unmounted device '%s'" % mp)

		if not uploaded:
			raise InvalidOperationException("Log files not present")
		else:
			log.debug("Uploaded %d files", uploaded)

	def datalogger_upload_all(self):
		""" Load all recorded data files from the Moku to the local PC.

		:raises NotDeployedException: if the instrument is not yet operational.
		:raises InvalidOperationException: if no files are present.
		"""
		import re

		if self._moku is None: raise NotDeployedException()

		uploaded = 0

		for mp in ['e', 'i']:
			try:
				files = self._moku._fs_list(mp)
				for f in files:
					if re.match(self.logname + ".*\.[a-z]{2,3}", f[0]):
						# Data length of zero uploads the whole file
						self._moku._receive_file(mp, f[0], 0)
						uploaded += 1
			except MPNotMounted:
				log.debug("Attempted to list files on unmounted device '%s'" % mp)

		if not uploaded:
			raise InvalidOperationException("Log files not present")
		else:
			log.debug("Uploaded %d files", uploaded)

	def datalogger_get_samples(self, timeout=None):
		""" Returns samples currently being streamed to the network.

		Requires a currently-running data logging session that has been started with the "net"
		file type.

		This function may return any number of samples, or an empty array in the case of timeout.
		In the case of a two-channel datalogging session, the sample array returned from any one
		call will only relate to one channel or the other. The first element of the return tuple
		will identify the channel.

		The second element of the return tuple is the index of the first data point relative to
		the whole log. This can be used to identify missing data and/or fill it from on-disk
		copies if the log is simultaneously hitting the network and disk.

		:type timeout: float
		:param timeout: Timeout in seconds

		:rtype: int, int, [ float, ... ]
		:return: The channel number, starting sample index, sample data array

		:raises NoDataException: if the logging session has stopped
		:raises FrameTimeout: if the timeout expired
		"""
		
		# If no network session exists, can't get samples
		if not self._dlskt:
			raise InvalidOperationException("No samples are being streamed to the network.")
		
		ch, start, coeff, raw = self._dl_get_samples_raw(timeout)

		self._strparser.set_coeff(ch, coeff)

		self._strparser.parse(raw, ch)
		parsed = self._strparser.processed[ch]
		self._strparser.clear_processed()

		return ch + 1, start, parsed


	def _dl_get_samples_raw(self, timeout):
		if self._dlskt in zmq.select([self._dlskt], [], [], timeout)[0]:
			hdr, data = self._dlskt.recv_multipart()

			hdr = hdr.decode('ascii')
			tag, ch, start, coeff = hdr.split('|')
			ch = int(ch)
			start = int(start)
			coeff = float(coeff)

			# Special value to indicate the stream has finished
			if ch == -1:
				raise NoDataException("Data log terminated")

			return ch, start, coeff, data
		else:
			raise FrameTimeout("Data log timed out after %d seconds", timeout)

	def set_running(self, state):
		prev_state = self._running
		super(FrameBasedInstrument, self).set_running(state)
		if state and not prev_state:
			self._fr_worker = threading.Thread(target=self._frame_worker)
			self._fr_worker.start()
		elif not state and prev_state:
			self._fr_worker.join()


	def _frame_worker(self):
		if(getattr(self, 'frame_class', None)):
			ctx = zmq.Context.instance()
			skt = ctx.socket(zmq.SUB)
			skt.connect("tcp://%s:27185" % self._moku._ip)
			skt.setsockopt_string(zmq.SUBSCRIBE, u'')
			skt.setsockopt(zmq.RCVHWM, 8)
			skt.setsockopt(zmq.LINGER, 5000)

			fr = self.frame_class(**self.frame_kwargs)

			try:
				while self._running:
					if skt in zmq.select([skt], [], [], 1.0)[0]:
						d = skt.recv()
						fr.add_packet(d)

						if fr.complete:
							self._queue.put_nowait(fr)
							fr = self.frame_class(**self.frame_kwargs)
			finally:
				skt.close()
