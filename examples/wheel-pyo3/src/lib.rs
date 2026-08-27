use pyo3::prelude::*;

#[pyfunction]
fn add(a: i64, b: i64) -> i64 {
    a + b
}

/// Runtime interpreter version — proves which interpreter actually loaded
/// this binary, not what it was compiled against.
#[pyfunction]
fn interpreter_version(py: Python<'_>) -> String {
    py.version().to_string()
}

#[pymodule]
fn demorust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(add, m)?)?;
    m.add_function(wrap_pyfunction!(interpreter_version, m)?)?;
    Ok(())
}
