/* Limited API only: one binary serves every CPython >= 3.9. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *interpreter_version(PyObject *self, PyObject *noargs) {
    /* Py_GetVersion() is runtime, not compile-time: proves the same binary
     * is really running under different interpreters. */
    return PyUnicode_FromString(Py_GetVersion());
}

static PyMethodDef methods[] = {
    {"interpreter_version", interpreter_version, METH_NOARGS,
     "Runtime interpreter version string."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "democaps", NULL, -1, methods,
};

PyMODINIT_FUNC PyInit_democaps(void) { return PyModule_Create(&module); }
