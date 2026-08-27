from setuptools import Extension, setup

setup(ext_modules=[Extension("demossl", ["demossl.c"], libraries=["ssl"])])
