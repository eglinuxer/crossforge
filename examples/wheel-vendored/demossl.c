#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/ssl.h>

static PyObject *tls_context_works(PyObject *self, PyObject *noargs) {
    SSL_CTX *ctx = SSL_CTX_new(TLS_method());
    if (!ctx)
        Py_RETURN_FALSE;
    SSL_CTX_free(ctx);
    Py_RETURN_TRUE;
}

static PyMethodDef methods[] = {
    {"tls_context_works", tls_context_works, METH_NOARGS,
     "Create and free a TLS context through the vendored libssl."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "demossl", NULL, -1, methods,
};

PyMODINIT_FUNC PyInit_demossl(void) { return PyModule_Create(&module); }
