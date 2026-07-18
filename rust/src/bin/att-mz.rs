//! Windows 便携发行包的最小启动器。

use std::env;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

const APP_HOME_ENV: &str = "ATT_MZ_HOME";

fn bundle_root(executable: &Path) -> Result<&Path, String> {
    executable
        .parent()
        .ok_or_else(|| "无法解析 att-mz.exe 所在目录".to_owned())
}

fn runtime_python(root: &Path) -> PathBuf {
    root.join("runtime").join("python.exe")
}

fn run() -> Result<i32, String> {
    let executable = env::current_exe().map_err(|error| format!("无法解析启动器路径：{error}"))?;
    let root = bundle_root(&executable)?;
    let python = runtime_python(root);
    if !python.is_file() {
        return Err(format!("发行包不完整，缺少运行时：{}", python.display()));
    }

    let mut command = Command::new(&python);
    command
        .arg("-I")
        .arg("-s")
        .arg("-m")
        .arg("app.cli_main")
        .args(env::args_os().skip(1))
        .env("PYTHONNOUSERSITE", "1")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH");
    if env::var_os(APP_HOME_ENV).is_none() {
        command.env(APP_HOME_ENV, root);
    }

    let status = command
        .status()
        .map_err(|error| format!("无法启动内置 Python 运行时：{error}"))?;
    Ok(status.code().unwrap_or(1))
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(u8::try_from(code).unwrap_or(1)),
        Err(message) => {
            eprintln!("att-mz 启动失败：{message}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{bundle_root, runtime_python};
    use std::path::Path;

    #[test]
    fn resolves_runtime_beside_launcher() {
        let executable = Path::new(r"C:\portable\att-mz\att-mz.exe");
        let root = bundle_root(executable).expect("launcher should have a parent");
        assert_eq!(
            runtime_python(root),
            Path::new(r"C:\portable\att-mz\runtime\python.exe")
        );
    }
}
