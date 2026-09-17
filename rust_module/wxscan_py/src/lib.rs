use std::io::Cursor;
use std::sync::{Mutex, MutexGuard};

use image::ImageReader;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use wxscan::backend::tract::TractNet;
use wxscan::WeChatQRCode;

#[pyclass]
struct Scanner {
    inner: Mutex<WeChatQRCode<TractNet>>,
}

#[pymethods]
impl Scanner {
    #[new]
    fn new(detect_model: &[u8], sr_model: &[u8]) -> PyResult<Self> {
        let detect = TractNet::from_bytes(detect_model)
            .map_err(|err| PyValueError::new_err(format!("failed to load detect model: {err}")))?;
        let sr = TractNet::from_bytes(sr_model)
            .map_err(|err| PyValueError::new_err(format!("failed to load super-resolution model: {err}")))?;
        Ok(Self {
            inner: Mutex::new(WeChatQRCode::new(Some(detect), Some(sr))),
        })
    }

    fn scan(&self, image_bytes: &[u8]) -> PyResult<Vec<String>> {
        let image = ImageReader::new(Cursor::new(image_bytes))
            .with_guessed_format()
            .map_err(|err| PyValueError::new_err(format!("invalid image: {err}")))?
            .decode()
            .map_err(|err| PyValueError::new_err(format!("failed to decode image: {err}")))?
            .to_luma8();
        let (width, height) = image.dimensions();
        let scanner: MutexGuard<'_, WeChatQRCode<TractNet>> = self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("wxscan scanner lock poisoned"))?;
        Ok(scanner
            .detect_and_decode_gray(image.as_raw(), width as usize, height as usize)
            .into_iter()
            .map(|result| result.text_lossy())
            .collect())
    }
}

#[pymodule]
fn fuckclassroom_wxscan(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Scanner>()?;
    Ok(())
}
