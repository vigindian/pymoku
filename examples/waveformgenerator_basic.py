#
# pymoku example: Basic Waveform Generator
#
# This example demonstrates how you can use the Waveform Generator instrument to
# generate an amplitude modulated sinewave on Channel 1, and un-modulated
# squarewave on Channel 2.
#
# (c) 2019 Liquid Instruments Pty. Ltd.
#
from pymoku import Moku
from pymoku.instruments import WaveformGenerator
import time

# Connect to your Moku by its device name
# Alternatively, use Moku.get_by_serial('#####') or Moku('192.168.###.###')
m = Moku.get_by_name('Moku')

try:
	i = m.deploy_or_connect(WaveformGenerator)

	# Generate a 1.0Vpp 1MHz Sinewave on Channel 1
	i.gen_sinewave(1, 1.0, 1e6)

	# Generate a 1.0Vpp 2MHz Squarewave on Channel 2
	# 30% Duty cycle, 10% Rise time, 10% Fall time
	i.gen_squarewave(2, 1.0, 2e6, risetime=0.1, falltime=0.1, duty=0.3)

	# Amplitude modulate the Channel 1 sinewave with another internally-generated sinewave.
	# 100% modulation depth at 10Hz.
	i.gen_modulate(1, 'amplitude', 'internal', 1, 10)

finally:
	m.close()
