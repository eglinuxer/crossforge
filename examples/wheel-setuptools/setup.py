from setuptools import Extension, setup

setup(ext_modules=[Extension("democore", ["democore.c"])])
