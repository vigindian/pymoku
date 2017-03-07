from distutils.core import setup
import subprocess, os

from pymoku import version

setup(
	name='pymoku',
	version=version.release,
	author='Ben Nizette',
	author_email='ben.nizette@liquidinstruments.com',
	packages=['pymoku',],
	license='Commercial',
	long_description=open('README.md').read(),

	entry_points={
		'console_scripts' : [
			'moku=pymoku.tools.moku:main'
		]
	}
)
