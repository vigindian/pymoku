# pymoku example: Basic FIR Filter Box
#
# This example demonstrates how you can configure the FIR Filter instrument.
#
# (c) 2018 Liquid Instruments Pty. Ltd.
#
from pymoku import Moku
from pymoku.instruments import FIRFilter
from scipy.signal import firwin
from scipy import fft
import math
import matplotlib.pyplot as plt

import logging
logging.basicConfig(level=logging.DEBUG)

# This script provides an example showing how to generate an FIR filter kernel with specified parameters using scipy and how to access settings of the FIR instrument.
# FIR kernels should have a normalised power of <= 1.0. Scipy's firwin function conforms to this requirement. 

## Specify nyquist and cutoff (-3dB) frequencies
#nyq_rate = 125e6 / 2**10 / 2.0
#cutoff_hz = 1e3

## Calculate FIR kernel using 10,000 taps and a chebyshev window with -60dB stop-band attenuation
#taps = firwin(1000, cutoff_hz/nyq_rate, window='hamming')
test=[]
for i in range(29):
	test += [(0/29.0)*(i+1)] * 511
#print taps, len(taps)
# This script provides a basic example showing how to load coefficients from an array into the FIRFilterBox.

# The following two example arrays are simple rectangular FIR kernels with 50 and 400 taps respectively. A rectangular kernel produces a sinc shaped transfer function with width
# inversely  proportional to the length of the kernel. FIR kernels must have a normalised power of <= 1.0, so the value of each tap is the inverse of the total number of taps.

#filt_coeff1 = [1.0 / 50.0] * 50
#filt_coeff2 = [1.0 / 400.0] * 400

# Connect to your Moku by its device name
# Alternatively, use Moku.get_by_serial('#####') or Moku('192.168.###.###')
m = Moku('192.168.69.249')
i = m.deploy_or_connect(FIRFilter)

try:
	#i._set_frontend(1, fiftyr=True, atten=False, ac=False)
	#i._set_frontend(2, fiftyr=True, atten=False, ac=False)

	# To implement 50 FIR taps we need a sample rate of 125 MHz / 2. To implement 400 FIR taps we need a sample rate of 125 MHz / 16.
	# Sample rate is configured according to: Fs = 125 MHz / 2^decimation_factor.
	i.set_filter(1, decimation_factor=10, filter_coefficients=test)
	#i.set_filter(2, decimation_factor=4, filter_coefficients=filt_coeff2)

	# Both channels have unity I/O scalars and no offsets. Channel 1 acts on ADC1 and channel 2 acts on ADC2.
	#i.set_offset_gain(ch=1, input_scale=1.0, output_scale=1.0, matrix_scalar_ch1=1.0, matrix_scalar_ch2=0.0)
	#i.set_offset_gain(ch=2, input_scale=1.0, output_scale=1.0, matrix_scalar_ch1=0.0, matrix_scalar_ch2=1.0)
	i._set_mmap_access(True)
	m._receive_file('j', '', 512*4*29, 'rand.dat')
	i._set_mmap_access(False)
	#m._receive_file('j', '', 512*4*29, 'tmp.dat')
finally:
	m.close()
