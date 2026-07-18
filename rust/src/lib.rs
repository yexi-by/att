//! Python 扩展入口。
//!
//! 本模块只暴露 PyO3 绑定，CPU 密集型规则计算集中放在 `native_core`。

mod native_core;
mod protocol;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

#[pyfunction]
fn native_contract() -> PyResult<String> {
    protocol::contract_json().map_err(PyRuntimeError::new_err)
}

#[pyfunction]
fn invoke(py: Python<'_>, request_json: String) -> PyResult<String> {
    py.detach(move || protocol::invoke_json(&request_json))
        .map_err(PyRuntimeError::new_err)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(native_contract, m)?)?;
    m.add_function(wrap_pyfunction!(invoke, m)?)?;
    Ok(())
}
