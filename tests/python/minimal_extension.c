#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *
crossforge_answer(PyObject *Py_UNUSED(module), PyObject *Py_UNUSED(ignored))
{
    return PyLong_FromLong(42);
}

static PyObject *
crossforge_wchar_roundtrip(PyObject *Py_UNUSED(module), PyObject *value)
{
    Py_ssize_t length;
    wchar_t *wide = PyUnicode_AsWideCharString(value, &length);
    if (wide == NULL) {
        return NULL;
    }

    PyObject *result = PyUnicode_FromWideChar(wide, length);
    PyMem_Free(wide);
    return result;
}

static PyMethodDef crossforge_methods[] = {
    {"answer", crossforge_answer, METH_NOARGS,
     PyDoc_STR("Return the qualification sentinel value.")},
    {"wchar_roundtrip", crossforge_wchar_roundtrip, METH_O,
     PyDoc_STR("Round-trip a string through the CPython wchar_t API.")},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef crossforge_module = {
    PyModuleDef_HEAD_INIT,
    "_crossforge",
    PyDoc_STR("Minimal Crossforge target-extension qualification module."),
    -1,
    crossforge_methods,
    NULL,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC
PyInit__crossforge(void)
{
    return PyModule_Create(&crossforge_module);
}
