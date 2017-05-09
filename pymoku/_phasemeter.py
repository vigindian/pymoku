
import math
import logging

from ._instrument import *
from . import _instrument
from . import _frame_instrument
from . import _stream_instrument
from . import _siggen
import _utils

from struct import unpack

import sys
# Annoying that import * doesn't pick up function defs??
_sgn = _instrument._sgn
_usgn = _instrument._usgn
_upsgn = _instrument._upsgn

log = logging.getLogger(__name__)

REG_PM_INITF1_H = 65
REG_PM_INITF1_L = 64
REG_PM_INITF2_L = 68
REG_PM_INITF2_H = 69
REG_PM_CGAIN = 66
REG_PM_INTSHIFT = 66
REG_PM_CSHIFT = 66
REG_PM_OUTDEC = 67
REG_PM_OUTSHIFT = 67
REG_PM_BW1 = 124
REG_PM_BW2 = 125
REG_PM_AUTOA1 = 126
REG_PM_AUTOA2 = 127

REG_PM_SG_EN = 96
REG_PM_SG_FREQ1_L = 97
REG_PM_SG_FREQ1_H = 98
REG_PM_SG_FREQ2_L = 99
REG_PM_SG_FREQ2_H = 100
REG_PM_SG_AMP = 105

# Phasemeter specific instrument constants
_PM_ADC_SMPS = _instrument.ADC_SMP_RATE
_PM_DAC_SMPS = _instrument.DAC_SMP_RATE
_PM_BUFLEN = _instrument.CHN_BUFLEN
_PM_FREQSCALE = 2.0**48 / _PM_DAC_SMPS
_PM_FREQ_MIN = 2e6
_PM_FREQ_MAX = 200e6
_PM_UPDATE_RATE = 1e6

_PM_CYCLE_SCALE = 2.0 * 2.0**16 / 2.0**48 * _PM_ADC_SMPS / _PM_UPDATE_RATE
_PM_HERTZ_SCALE = 2.0 * _PM_ADC_SMPS / 2**48
_PM_VOLTS_SCALE = 2.0 / (_PM_ADC_SMPS * _PM_ADC_SMPS / _PM_UPDATE_RATE / _PM_UPDATE_RATE)

# Phasemeter signal generator constants
_PM_SG_AMPSCALE = 2**16 / 4.0
_PM_SG_FREQSCALE = _PM_FREQSCALE

# Pre-defined log rates which ensure samplerate will set to ~120Hz or ~30Hz
_PM_LOGRATE_FAST = 123
_PM_LOGRATE_SLOW = 31

class Phasemeter_SignalGenerator(MokuInstrument):
	def __init__(self):
		super(Phasemeter_SignalGenerator, self).__init__()
		self._register_accessors(_pm_siggen_reg_hdl)

	@needs_commit
	def set_defaults(self):
		# Turn off generated output waves
		self.gen_off()

	@needs_commit
	def gen_sinewave(self, ch, amplitude, frequency):
		"""
		:param ch: Channel number
		:param amplitude: Signal amplitude in volts
		:param frequency: Frequency in Hz
		"""
		if ch == 1:
			self.pm_out1_frequency = frequency
			self.pm_out1_amplitude = amplitude
		if ch == 2:
			self.pm_out2_frequency = frequency
			self.pm_out2_amplitude = amplitude

	@needs_commit
	def gen_off(self, ch=None):
		""" Turn Signal Generator output(s) off.

		The channel will be turned on when configuring the waveform type but can be turned off
		using this function. If *ch* is None (the default), both channels will be turned off,
		otherwise just the one specified by the argument.

		:type ch: int
		:param ch: Channel to turn off
		"""
		if (ch is None) or ch == 1:
			self.pm_out1_amplitude = 0

		if (ch is None) or ch == 2:
			self.pm_out2_amplitude = 0


_pm_siggen_reg_hdl = {
	'pm_out1_frequency':	((REG_PM_SG_FREQ1_H, REG_PM_SG_FREQ1_L),
											to_reg_unsigned(0, 48, xform=lambda obj, f:f * _PM_SG_FREQSCALE ),
											from_reg_unsigned(0, 48, xform=lambda obj, f: f / _PM_FREQSCALE )),
	'pm_out2_frequency':	((REG_PM_SG_FREQ2_H, REG_PM_SG_FREQ2_L),
											to_reg_unsigned(0, 48, xform=lambda obj, f:f * _PM_SG_FREQSCALE ),
											from_reg_unsigned(0, 48, xform=lambda obj, f: f /_PM_FREQSCALE )),
	'pm_out1_amplitude':	(REG_PM_SG_AMP, to_reg_unsigned(0, 16, xform=lambda obj, a: a / obj._dac_gains()[0]),
											from_reg_unsigned(0,16, xform=lambda obj, a: a * obj._dac_gains()[0])),
	'pm_out2_amplitude':	(REG_PM_SG_AMP, to_reg_unsigned(16, 16, xform=lambda obj, a: a / obj._dac_gains()[1]),
											from_reg_unsigned(16,16, xform=lambda obj, a: a * obj._dac_gains()[1]))
}

class Phasemeter(_stream_instrument.StreamBasedInstrument, Phasemeter_SignalGenerator): #TODO Frame instrument may not be appropriate when we get streaming going.
	""" Phasemeter instrument object. This should be instantiated and attached to a :any:`Moku` instance.

	.. automethod:: pymoku.instruments.Phasemeter.__init__

	.. attribute:: type
		:annotation: = "phasemeter"

		Name of this instrument.

	"""
	def __init__(self):
		"""Create a new Phasemeter instrument, ready to be attached to a Moku."""
		super(Phasemeter, self).__init__()
		self._register_accessors(_pm_reg_handlers)

		self.id = 3
		self.type = "phasemeter"
		self.logname = "MokuPhasemeterData"

		self.binstr = "<p32,0xAAAAAAAA:u48:u48:s15:p1,0:s48:s32:s32"
		self.procstr = ["*{:.16e} : *{:.16e} : : *{:.16e} : *C*{:.16e} : *C*{:.16e} ".format(_PM_HERTZ_SCALE, _PM_HERTZ_SCALE,  _PM_CYCLE_SCALE, _PM_VOLTS_SCALE, _PM_VOLTS_SCALE),
						"*{:.16e} : *{:.16e} : : *{:.16e} : *C*{:.16e} : *C*{:.16e} ".format(_PM_HERTZ_SCALE, _PM_HERTZ_SCALE,  _PM_CYCLE_SCALE, _PM_VOLTS_SCALE, _PM_VOLTS_SCALE)]

	def _update_datalogger_params(self):
		# Call this function when any instrument configuration parameters are set
		self.fmtstr = self._get_fmtstr(self.ch1,self.ch2)
		self.hdrstr = self._get_hdrstr(self.ch1,self.ch2)

	@needs_commit
	def set_samplerate(self, samplerate):
		""" Manually set the sample rate of the Phasemeter.

		The chosen samplerate will be rounded down to nearest allowable rate
		based on R(Hz) = 1e6/(2^N) where N in range [13,16].

		Alternatively use samplerate = {'slow','fast'}
		to set ~30Hz or ~120Hz.

		:type samplerate: float, or string = {'slow','fast'}
		:param samplerate: Desired sample rate
		"""
		if type(samplerate) is str:
			_str_to_samplerate = {
				'slow' : _PM_LOGRATE_SLOW,
				'fast' : _PM_LOGRATE_FAST
			}
			samplerate = _utils.str_to_val(_str_to_samplerate, samplerate, 'samplerate')
		new_samplerate = _PM_UPDATE_RATE/min(max(1,samplerate),200)
		shift = min(math.ceil(math.log(new_samplerate,2)),16)
		self.output_decimation = 2**shift
		self.output_shift = shift
		self.timestep = 1.0/(_PM_UPDATE_RATE/self.output_decimation)
		log.info("Samplerate set to %.2f Hz", _PM_UPDATE_RATE/float(self.output_decimation) )

	def get_samplerate(self):
		"""
		Get the current output sample rate of the phase meter.
		"""
		return _PM_UPDATE_RATE / self.output_decimation

	@needs_commit
	def set_initfreq(self, ch, f):
		""" Manually set the initial frequency of the designated channel

		:type ch: int; *{1,2}*
		:param ch: Channel number to set the initial frequency of.

		:type f: int; *2e6 < f < 200e6*
		:param f: Initial locking frequency of the designated channel

		"""
		if _PM_FREQ_MIN <= f <= _PM_FREQ_MAX:
			if ch == 1:
				self.init_freq_ch1 = int(f);
			elif ch == 2:
				self.init_freq_ch2 = int(f);
			else:
				raise ValueError("Invalid channel number")
		else:
			raise ValueError("Initial frequency is not within the valid range.")

	def get_initfreq(self, ch):
		"""
		Reads the seed frequency register of the phase tracking loop
		Valid if auto acquire has not been used

		:type ch: int; *{1,2}*
		:param ch: Channel number to read the initial frequency of.
		"""
		if ch == 1:
			return self.init_freq_ch1
		elif ch == 2:
			return self.init_freq_ch2
		else:
			raise ValueError("Invalid channel number.")

	def _set_controlgain(self, v):
		#TODO: Put limits on the range of 'v'
		self.control_gain = v

	def _get_controlgain(self):
		return self.control_gain

	@needs_commit
	def set_bandwidth(self, ch, bw):
		"""
		Set the bandwidth of an ADC channel

		:type ch: int; *{1,2}*
		:param ch: ADC channel number to set bandwidth of.

		:type bw: float; Hz
		:param n: Desired bandwidth (will be rounded up to to the nearest multiple 10kHz * 2^N with N = [-6,0])
		"""
		if bw <= 0:
			raise ValueError("Invalid bandwidth (must be positive).")
		n = min(max(math.ceil(math.log(bw/10e3,2)),-6),0)

		if ch == 1:
			self.bandwidth_ch1 = n
		elif ch == 2:
			self.bandwidth_ch2 = n

	def get_bandwidth(self, ch):
		return 10e3 * (2**(self.bandwidth_ch1 if ch == 1 else self.bandwidth_ch2))

	@needs_commit
	def auto_acquire(self, ch):
		"""
		Auto-acquire the initial frequency of the specified channel

		:type ch: int; *{1,2}*
		:param ch: Channel number
		"""
		if ch == 1:
			self.autoacquire_ch1 = True
		elif ch == 2:
			self.autoacquire_ch2 = True
		else:
			raise ValueError("Invalid channel")

	def _get_hdrstr(self, ch1, ch2):
		chs = [ch1, ch2]

		hdr =  "% Moku:Phasemeter \r\n"
		for i,c in enumerate(chs):
			if c:
				r = self.get_frontend(i+1)
				hdr += "% Ch {i} - {} coupling, {} Ohm impedance, {} V range\r\n".format("AC" if r[2] else "DC", "50" if r[0] else "1M", "10" if r[1] else "1", i=i+1 )

		hdr += "%"
		for i,c in enumerate(chs):
			if c:
				hdr += "{} Ch {i} bandwidth = {:.10e} (Hz)".format("," if ((ch1 and ch2) and i == 1) else "", self.get_bandwidth(i+1), i=i+1)
		hdr += "\r\n"

		hdr += "% Acquisition rate: {:.10e} Hz\r\n".format(self.get_samplerate())
		hdr += "% {} 10 MHz clock\r\n".format("External" if self._moku._get_actual_extclock() else "Internal")
		hdr += "% Acquired {}\r\n".format(_utils.formatted_timestamp())
		hdr += "% Time,"
		for i,c in enumerate(chs):
			if c:
				hdr += "{} Set frequency {i} (Hz), Frequency {i} (Hz), Phase {i} (cyc), I {i} (V), Q {i} (V)".format("," if ((ch1 and ch2) and i == 1) else "", i=i+1)

		hdr += "\r\n"

		return hdr

	def _get_fmtstr(self, ch1, ch2):
		fmtstr = "{t:.10e}"
		if ch1:
			fmtstr += ", {ch1[0]:.16e}, {ch1[1]:.16e}, {ch1[3]:.16e}, {ch1[4]:.10e}, {ch1[5]:.10e}"
		if ch2:
			fmtstr += ", {ch2[0]:.16e}, {ch2[1]:.16e}, {ch2[3]:.16e}, {ch2[4]:.10e}, {ch2[5]:.10e}"
		fmtstr += "\r\n"
		return fmtstr

	@needs_commit
	def set_defaults(self):
		super(Phasemeter, self).set_defaults()

		# Because we have to deal with a "frame" type instrument
		self.x_mode = _instrument.ROLL
		self.framerate = 0

		# Set basic configurations
		self.set_samplerate(1e3)
		self.set_initfreq(1, 10e6)
		self.set_initfreq(2, 10e6)

		# Set PI controller gains
		self._set_controlgain(100)
		self.control_shift = 0
		self.integrator_shift = 0
		self.output_shift = math.log(self.output_decimation,2)

		# Configuring the relays for impedance, voltage range etc.
		self.set_frontend(1, fiftyr=True, atten=True, ac=True)
		self.set_frontend(2, fiftyr=True, atten=True, ac=True)

		self.en_in_ch1 = True
		self.en_in_ch2 = True


	def _on_sync_regs(self):
		self.timestep = 1.0/(_PM_UPDATE_RATE/self.output_decimation)


_pm_reg_handlers = {
	'init_freq_ch1':		((REG_PM_INITF1_H, REG_PM_INITF1_L),
											to_reg_unsigned(0,48, xform=lambda obj, f: f * _PM_FREQSCALE),
											from_reg_unsigned(0,48,xform=lambda obj, f: f / _PM_FREQSCALE)),
	'init_freq_ch2':		((REG_PM_INITF2_H, REG_PM_INITF2_L),
											to_reg_unsigned(0,48, xform=lambda obj, f: f * _PM_FREQSCALE),
											from_reg_unsigned(0,48,xform=lambda obj, f: f / _PM_FREQSCALE)),
	'control_gain':			(REG_PM_CGAIN,	to_reg_signed(0,16),
											from_reg_signed(0,16)),
	'control_shift':		(REG_PM_CGAIN,	to_reg_unsigned(20,4),
											from_reg_unsigned(20,4)),
	'integrator_shift':		(REG_PM_INTSHIFT, to_reg_unsigned(16,4),
											from_reg_unsigned(16,4)),
	'output_decimation':	(REG_PM_OUTDEC,	to_reg_unsigned(0,17),
											from_reg_unsigned(0,17)),
	'output_shift':			(REG_PM_OUTSHIFT, to_reg_unsigned(17,5),
											from_reg_unsigned(17,5)),
	'bandwidth_ch1':		(REG_PM_BW1, to_reg_signed(0,5, xform=lambda obj, b: b),
											from_reg_signed(0,5, xform=lambda obj, b: b)),
	'bandwidth_ch2':		(REG_PM_BW2, to_reg_signed(0,5, xform=lambda obj, b: b),
											from_reg_signed(0,5, xform=lambda obj, b: b)),
	'autoacquire_ch1':		(REG_PM_AUTOA1, to_reg_bool(0), from_reg_bool(0)),
	'autoacquire_ch2': 		(REG_PM_AUTOA2, to_reg_bool(0), from_reg_bool(0))
}
