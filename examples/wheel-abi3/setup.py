from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            "democaps",
            ["democaps.c"],
            py_limited_api=True,
            define_macros=[("Py_LIMITED_API", "0x03090000")],
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp39"}},
)
