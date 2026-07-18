//! 受根目录约束的批量文件 SHA-256。
//!
//! 本模块只接受严格相对路径，逐层拒绝符号链接和 Windows 重解析点，
//! 并在共享 Rayon 池中并行流式读取普通文件。

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, Metadata};
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use super::pool::run_with_optional_pool;

const READ_BUFFER_SIZE: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HashFilesPayload {
    root: String,
    files: Vec<HashFileInput>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HashFileInput {
    id: String,
    relative_path: String,
}

#[derive(Debug, Serialize)]
struct HashFilesOutput {
    files: Vec<HashFileOutput>,
}

#[derive(Debug, Serialize)]
struct HashFileOutput {
    id: String,
    relative_path: String,
    sha256: String,
    byte_size: u64,
}

#[derive(Debug)]
struct ValidatedInput {
    id: String,
    relative_path: String,
    path_parts: Vec<String>,
}

#[derive(Debug)]
struct PreparedInput {
    id: String,
    relative_path: String,
    absolute_path: PathBuf,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct MetadataSnapshot {
    byte_size: u64,
    #[cfg(windows)]
    creation_time: u64,
    #[cfg(windows)]
    last_write_time: u64,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(unix)]
    modified_seconds: i64,
    #[cfg(unix)]
    modified_nanoseconds: i64,
    #[cfg(unix)]
    changed_seconds: i64,
    #[cfg(unix)]
    changed_nanoseconds: i64,
}

impl MetadataSnapshot {
    fn from_metadata(metadata: &Metadata) -> Self {
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            Self {
                byte_size: metadata.len(),
                creation_time: metadata.creation_time(),
                last_write_time: metadata.last_write_time(),
            }
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            Self {
                byte_size: metadata.len(),
                device: metadata.dev(),
                inode: metadata.ino(),
                modified_seconds: metadata.mtime(),
                modified_nanoseconds: metadata.mtime_nsec(),
                changed_seconds: metadata.ctime(),
                changed_nanoseconds: metadata.ctime_nsec(),
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct FileIdentity {
    #[cfg(windows)]
    volume_serial_number: u32,
    #[cfg(windows)]
    file_index: u64,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
}

impl FileIdentity {
    fn from_file(file: &File) -> io::Result<Self> {
        #[cfg(windows)]
        {
            windows_file_identity(file)
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let metadata = file.metadata()?;
            Ok(Self {
                device: metadata.dev(),
                inode: metadata.ino(),
            })
        }
    }
}

struct StabilityObservations<'a> {
    path_before: &'a MetadataSnapshot,
    handle_before: &'a MetadataSnapshot,
    handle_after: &'a MetadataSnapshot,
    path_after: &'a MetadataSnapshot,
    identity_before: &'a FileIdentity,
    identity_after: &'a FileIdentity,
    path_after_identity: &'a FileIdentity,
    read_size: u64,
}

#[cfg(windows)]
#[repr(C)]
struct WindowsFileTime {
    _low_date_time: u32,
    _high_date_time: u32,
}

#[cfg(windows)]
#[repr(C)]
struct WindowsByHandleFileInformation {
    _file_attributes: u32,
    _creation_time: WindowsFileTime,
    _last_access_time: WindowsFileTime,
    _last_write_time: WindowsFileTime,
    volume_serial_number: u32,
    _file_size_high: u32,
    _file_size_low: u32,
    _number_of_links: u32,
    file_index_high: u32,
    file_index_low: u32,
}

#[cfg(windows)]
#[link(name = "Kernel32")]
unsafe extern "system" {
    fn GetFileInformationByHandle(
        file: *mut std::ffi::c_void,
        information: *mut WindowsByHandleFileInformation,
    ) -> i32;
}

#[cfg(windows)]
fn windows_file_identity(file: &File) -> io::Result<FileIdentity> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;

    let mut information = MaybeUninit::<WindowsByHandleFileInformation>::uninit();
    // SAFETY: `file` 在调用期间保持打开；输出指针指向足够大的未初始化结构，
    // Windows 仅在返回非零时保证完整写入，失败时不会读取该结构。
    let succeeded =
        unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) };
    if succeeded == 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: GetFileInformationByHandle 已返回成功，按 API 契约写满输出结构。
    let information = unsafe { information.assume_init() };
    Ok(FileIdentity {
        volume_serial_number: information.volume_serial_number,
        file_index: (u64::from(information.file_index_high) << 32)
            | u64::from(information.file_index_low),
    })
}

/// `hash.files` 可由协议层稳定映射的结构化失败。
#[derive(Debug)]
pub(crate) struct FileHashError {
    pub(crate) code: &'static str,
    pub(crate) stage: &'static str,
    pub(crate) message: String,
    pub(crate) details: BTreeMap<String, Value>,
}

impl FileHashError {
    fn new(
        code: &'static str,
        stage: &'static str,
        message: impl Into<String>,
        details: BTreeMap<String, Value>,
    ) -> Self {
        Self {
            code,
            stage,
            message: message.into(),
            details,
        }
    }
}

pub(crate) fn hash_files_impl(payload_json: &str) -> Result<String, FileHashError> {
    let payload: HashFilesPayload = serde_json::from_str(payload_json).map_err(|error| {
        FileHashError::new(
            "hash_files_invalid_payload",
            "decode",
            "hash.files payload 结构无效",
            details([("reason", Value::String(error.to_string()))]),
        )
    })?;
    let validated_inputs = validate_inputs(payload.files)?;
    let root = validate_root(&payload.root)?;
    let prepared_inputs = validated_inputs
        .into_iter()
        .map(|input| prepare_input(&root, input))
        .collect::<Result<Vec<_>, _>>()?;

    let outcomes = run_with_optional_pool(|| {
        prepared_inputs
            .par_iter()
            .map(hash_prepared_input)
            .collect::<Vec<_>>()
    })
    .map_err(|error| {
        FileHashError::new(
            "hash_files_executor_failed",
            "execute",
            "hash.files 共享线程池执行失败",
            details([("reason", Value::String(error))]),
        )
    })?;

    // Rayon 的 indexed collect 保留输入顺序；这里再按顺序展开 Result，
    // 保证多个文件同时失败时也固定报告请求中最靠前的一项。
    let files = outcomes.into_iter().collect::<Result<Vec<_>, _>>()?;
    serde_json::to_string(&HashFilesOutput { files }).map_err(|error| {
        FileHashError::new(
            "hash_files_encode_failed",
            "encode",
            "hash.files 结果编码失败",
            details([("reason", Value::String(error.to_string()))]),
        )
    })
}

fn validate_inputs(inputs: Vec<HashFileInput>) -> Result<Vec<ValidatedInput>, FileHashError> {
    let mut ids = HashSet::with_capacity(inputs.len());
    let mut paths = HashSet::with_capacity(inputs.len());
    let mut validated = Vec::with_capacity(inputs.len());
    for input in inputs {
        if input.id.trim().is_empty() || input.id.chars().any(char::is_control) {
            return Err(input_error(
                "hash_files_invalid_id",
                "hash.files 文件 id 不能为空或包含控制字符",
                &input,
                "id_invalid",
            ));
        }
        if !ids.insert(input.id.clone()) {
            return Err(input_error(
                "hash_files_duplicate_id",
                "hash.files 文件 id 重复",
                &input,
                "duplicate_id",
            ));
        }
        let (normalized_path, path_parts) = normalize_relative_path(&input)?;
        let duplicate_key = duplicate_path_key(&normalized_path);
        if !paths.insert(duplicate_key) {
            return Err(input_error(
                "hash_files_duplicate_path",
                "hash.files 相对路径重复",
                &input,
                "duplicate_relative_path",
            ));
        }
        validated.push(ValidatedInput {
            id: input.id,
            relative_path: input.relative_path,
            path_parts,
        });
    }
    Ok(validated)
}

fn normalize_relative_path(input: &HashFileInput) -> Result<(String, Vec<String>), FileHashError> {
    let raw_path = input.relative_path.as_str();
    if raw_path.is_empty() {
        return Err(input_error(
            "hash_files_invalid_relative_path",
            "hash.files relative_path 不能为空",
            input,
            "empty",
        ));
    }
    if raw_path.starts_with('/')
        || raw_path.starts_with('\\')
        || raw_path.as_bytes().get(1) == Some(&b':')
        || Path::new(raw_path).is_absolute()
    {
        return Err(input_error(
            "hash_files_invalid_relative_path",
            "hash.files relative_path 必须是相对路径",
            input,
            "absolute_or_prefixed",
        ));
    }
    let path_parts = raw_path
        .split(['/', '\\'])
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if path_parts
        .iter()
        .any(|part| part.is_empty() || part == "." || part == ".." || part.contains(':'))
    {
        return Err(input_error(
            "hash_files_invalid_relative_path",
            "hash.files relative_path 包含空段、`.`、`..` 或冒号",
            input,
            "unsafe_component",
        ));
    }
    Ok((path_parts.join("/"), path_parts))
}

fn duplicate_path_key(normalized_path: &str) -> String {
    #[cfg(windows)]
    {
        normalized_path.to_lowercase()
    }
    #[cfg(not(windows))]
    {
        normalized_path.to_owned()
    }
}

fn validate_root(root_text: &str) -> Result<PathBuf, FileHashError> {
    if root_text.trim().is_empty() {
        return Err(root_error(
            "hash_files_invalid_root",
            "hash.files root 不能为空",
            root_text,
            "empty",
        ));
    }
    let root = Path::new(root_text);
    if !root.is_absolute() {
        return Err(root_error(
            "hash_files_invalid_root",
            "hash.files root 必须是绝对路径",
            root_text,
            "not_absolute",
        ));
    }
    reject_link_ancestors(root, None)?;
    let metadata =
        fs::symlink_metadata(root).map_err(|error| root_metadata_error(root_text, &error))?;
    if metadata_is_link_like(&metadata) {
        return Err(root_error(
            "hash_files_link_not_allowed",
            "hash.files root 不允许是符号链接或目录联接",
            root_text,
            "root_link",
        ));
    }
    if !metadata.is_dir() {
        return Err(root_error(
            "hash_files_root_not_directory",
            "hash.files root 不是目录",
            root_text,
            "not_directory",
        ));
    }
    root.canonicalize().map_err(|error| {
        root_error_with_io(
            "hash_files_root_metadata_failed",
            "hash.files root 规范化失败",
            root_text,
            &error,
        )
    })
}

fn reject_link_ancestors(path: &Path, input: Option<&ValidatedInput>) -> Result<(), FileHashError> {
    let ancestors = path.ancestors().collect::<Vec<_>>();
    for ancestor in ancestors.into_iter().rev() {
        if ancestor.as_os_str().is_empty() {
            continue;
        }
        let metadata = match fs::symlink_metadata(ancestor) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => {
                return Err(match input {
                    Some(input) => file_io_error(
                        "hash_files_metadata_failed",
                        "hash.files 无法读取路径元数据",
                        input,
                        &error,
                    ),
                    None => root_error_with_io(
                        "hash_files_root_metadata_failed",
                        "hash.files 无法读取 root 路径元数据",
                        &path.to_string_lossy(),
                        &error,
                    ),
                });
            }
        };
        if metadata_is_link_like(&metadata) {
            let component = ancestor.to_string_lossy().into_owned();
            return Err(match input {
                Some(input) => FileHashError::new(
                    "hash_files_link_not_allowed",
                    "validate",
                    "hash.files 路径不允许经过符号链接或目录联接",
                    details([
                        ("id", Value::String(input.id.clone())),
                        ("relative_path", Value::String(input.relative_path.clone())),
                        ("component", Value::String(component)),
                    ]),
                ),
                None => FileHashError::new(
                    "hash_files_link_not_allowed",
                    "validate",
                    "hash.files root 不允许经过符号链接或目录联接",
                    details([
                        ("root", Value::String(path.to_string_lossy().into_owned())),
                        ("component", Value::String(component)),
                    ]),
                ),
            });
        }
    }
    Ok(())
}

fn prepare_input(root: &Path, input: ValidatedInput) -> Result<PreparedInput, FileHashError> {
    let mut current = root.to_path_buf();
    for (index, part) in input.path_parts.iter().enumerate() {
        current.push(part);
        let metadata = fs::symlink_metadata(&current).map_err(|error| {
            if error.kind() == io::ErrorKind::NotFound {
                file_io_error(
                    "hash_files_file_not_found",
                    "hash.files 文件或路径组件不存在",
                    &input,
                    &error,
                )
            } else {
                file_io_error(
                    "hash_files_metadata_failed",
                    "hash.files 无法读取文件元数据",
                    &input,
                    &error,
                )
            }
        })?;
        if metadata_is_link_like(&metadata) {
            return Err(FileHashError::new(
                "hash_files_link_not_allowed",
                "validate",
                "hash.files 路径不允许经过符号链接或目录联接",
                details([
                    ("id", Value::String(input.id.clone())),
                    ("relative_path", Value::String(input.relative_path.clone())),
                    ("component", Value::String(part.clone())),
                ]),
            ));
        }
        let is_last = index + 1 == input.path_parts.len();
        if !is_last && !metadata.is_dir() {
            return Err(input_error_from_validated(
                "hash_files_parent_not_directory",
                "hash.files 路径组件不是目录",
                &input,
                "parent_not_directory",
            ));
        }
        if is_last && !metadata.is_file() {
            return Err(input_error_from_validated(
                "hash_files_not_regular_file",
                "hash.files 目标不是普通文件",
                &input,
                "not_regular_file",
            ));
        }
    }
    reject_link_ancestors(&current, Some(&input))?;
    let absolute_path = current.canonicalize().map_err(|error| {
        file_io_error(
            "hash_files_metadata_failed",
            "hash.files 文件规范化失败",
            &input,
            &error,
        )
    })?;
    if !absolute_path.starts_with(root) {
        return Err(input_error_from_validated(
            "hash_files_path_outside_root",
            "hash.files 文件路径越出 root",
            &input,
            "outside_root",
        ));
    }
    Ok(PreparedInput {
        id: input.id,
        relative_path: input.relative_path,
        absolute_path,
    })
}

fn hash_prepared_input(input: &PreparedInput) -> Result<HashFileOutput, FileHashError> {
    let link_metadata = fs::symlink_metadata(&input.absolute_path).map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法在读取前确认文件元数据",
            input,
            &error,
        )
    })?;
    if metadata_is_link_like(&link_metadata) {
        return Err(prepared_error(
            "hash_files_link_not_allowed",
            "hash.files 文件在读取前变成了符号链接或目录联接",
            input,
            "link_before_read",
        ));
    }
    if !link_metadata.is_file() {
        return Err(prepared_error(
            "hash_files_not_regular_file",
            "hash.files 目标在读取前不再是普通文件",
            input,
            "not_regular_file_before_read",
        ));
    }
    let path_before = MetadataSnapshot::from_metadata(&link_metadata);

    let mut file = open_file_for_hash(&input.absolute_path).map_err(|error| {
        prepared_io_error(
            "hash_files_read_failed",
            "hash.files 无法打开文件",
            input,
            &error,
        )
    })?;
    let before = file.metadata().map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取已打开文件的元数据",
            input,
            &error,
        )
    })?;
    let handle_before = MetadataSnapshot::from_metadata(&before);
    let identity_before = FileIdentity::from_file(&file).map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取已打开文件的身份",
            input,
            &error,
        )
    })?;
    if path_before != handle_before {
        return Err(prepared_error(
            "hash_files_file_changed",
            "hash.files 在打开句柄前路径元数据发生变化",
            input,
            "path_changed_before_read",
        ));
    }
    let mut hasher = Sha256::new();
    let mut byte_size = 0_u64;
    let mut buffer = vec![0_u8; READ_BUFFER_SIZE];
    loop {
        let read_count = file.read(&mut buffer).map_err(|error| {
            prepared_io_error(
                "hash_files_read_failed",
                "hash.files 读取文件失败",
                input,
                &error,
            )
        })?;
        if read_count == 0 {
            break;
        }
        hasher.update(&buffer[..read_count]);
        byte_size = byte_size.checked_add(read_count as u64).ok_or_else(|| {
            prepared_error(
                "hash_files_size_overflow",
                "hash.files 文件字节数超出 u64",
                input,
                "byte_size_overflow",
            )
        })?;
    }
    let after = file.metadata().map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取哈希后的文件元数据",
            input,
            &error,
        )
    })?;
    let handle_after = MetadataSnapshot::from_metadata(&after);
    let identity_after = FileIdentity::from_file(&file).map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取哈希后的文件身份",
            input,
            &error,
        )
    })?;
    let path_after_metadata = fs::symlink_metadata(&input.absolute_path).map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取哈希后的当前路径元数据",
            input,
            &error,
        )
    })?;
    if metadata_is_link_like(&path_after_metadata) || !path_after_metadata.is_file() {
        return Err(prepared_error(
            "hash_files_file_changed",
            "hash.files 读取期间当前路径不再指向普通文件",
            input,
            "path_type_changed",
        ));
    }
    let path_after = MetadataSnapshot::from_metadata(&path_after_metadata);
    let path_after_file = open_file_for_hash(&input.absolute_path).map_err(|error| {
        prepared_io_error(
            "hash_files_read_failed",
            "hash.files 无法重新打开当前路径核验文件身份",
            input,
            &error,
        )
    })?;
    let path_after_handle =
        MetadataSnapshot::from_metadata(&path_after_file.metadata().map_err(|error| {
            prepared_io_error(
                "hash_files_metadata_failed",
                "hash.files 无法读取当前路径句柄元数据",
                input,
                &error,
            )
        })?);
    if path_after != path_after_handle {
        return Err(prepared_error(
            "hash_files_file_changed",
            "hash.files 在最终核验当前路径时元数据发生变化",
            input,
            "path_changed_during_final_check",
        ));
    }
    let path_after_identity = FileIdentity::from_file(&path_after_file).map_err(|error| {
        prepared_io_error(
            "hash_files_metadata_failed",
            "hash.files 无法读取当前路径的文件身份",
            input,
            &error,
        )
    })?;
    verify_metadata_stability(
        input,
        &StabilityObservations {
            path_before: &path_before,
            handle_before: &handle_before,
            handle_after: &handle_after,
            path_after: &path_after,
            identity_before: &identity_before,
            identity_after: &identity_after,
            path_after_identity: &path_after_identity,
            read_size: byte_size,
        },
    )?;
    // 明确在结果离开工作线程前释放禁止写入/替换的 Windows 句柄。
    drop(path_after_file);
    drop(file);
    Ok(HashFileOutput {
        id: input.id.clone(),
        relative_path: input.relative_path.clone(),
        sha256: format!("{:x}", hasher.finalize()),
        byte_size,
    })
}

#[cfg(windows)]
fn open_file_for_hash(path: &Path) -> io::Result<File> {
    use std::fs::OpenOptions;
    use std::os::windows::fs::OpenOptionsExt;

    const FILE_SHARE_READ: u32 = 0x0000_0001;
    OpenOptions::new()
        .read(true)
        // Windows 上拒绝同时写入和替换；读取完成后仍会比较文件身份与 mtime。
        .share_mode(FILE_SHARE_READ)
        .open(path)
}

#[cfg(not(windows))]
fn open_file_for_hash(path: &Path) -> io::Result<File> {
    File::open(path)
}

fn verify_metadata_stability(
    input: &PreparedInput,
    observations: &StabilityObservations<'_>,
) -> Result<(), FileHashError> {
    if observations.path_before != observations.handle_before
        || observations.handle_before != observations.handle_after
        || observations.handle_after != observations.path_after
        || observations.identity_before != observations.identity_after
        || observations.identity_after != observations.path_after_identity
        || observations.handle_before.byte_size != observations.read_size
    {
        return Err(file_changed_error(
            input,
            "identity_size_or_mtime_changed",
            observations,
        ));
    }
    Ok(())
}

fn file_changed_error(
    input: &PreparedInput,
    reason: &'static str,
    observations: &StabilityObservations<'_>,
) -> FileHashError {
    FileHashError::new(
        "hash_files_file_changed",
        "execute",
        "hash.files 读取期间文件身份、大小或修改时间发生变化",
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("reason", Value::String(reason.to_owned())),
            (
                "path_before_size",
                Value::from(observations.path_before.byte_size),
            ),
            (
                "handle_before_size",
                Value::from(observations.handle_before.byte_size),
            ),
            (
                "handle_after_size",
                Value::from(observations.handle_after.byte_size),
            ),
            (
                "path_after_size",
                Value::from(observations.path_after.byte_size),
            ),
            ("read_size", Value::from(observations.read_size)),
            (
                "path_binding_changed",
                Value::Bool(
                    observations.path_before != observations.handle_before
                        || observations.handle_after != observations.path_after,
                ),
            ),
            (
                "opened_file_metadata_changed",
                Value::Bool(observations.handle_before != observations.handle_after),
            ),
            (
                "file_identity_changed",
                Value::Bool(
                    observations.identity_before != observations.identity_after
                        || observations.identity_after != observations.path_after_identity,
                ),
            ),
        ]),
    )
}

fn metadata_is_link_like(metadata: &Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}

fn input_error(
    code: &'static str,
    message: &'static str,
    input: &HashFileInput,
    reason: &'static str,
) -> FileHashError {
    FileHashError::new(
        code,
        "validate",
        message,
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("reason", Value::String(reason.to_owned())),
        ]),
    )
}

fn input_error_from_validated(
    code: &'static str,
    message: &'static str,
    input: &ValidatedInput,
    reason: &'static str,
) -> FileHashError {
    FileHashError::new(
        code,
        "validate",
        message,
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("reason", Value::String(reason.to_owned())),
        ]),
    )
}

fn prepared_error(
    code: &'static str,
    message: &'static str,
    input: &PreparedInput,
    reason: &'static str,
) -> FileHashError {
    FileHashError::new(
        code,
        "execute",
        message,
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("reason", Value::String(reason.to_owned())),
        ]),
    )
}

fn root_error(
    code: &'static str,
    message: &'static str,
    root: &str,
    reason: &'static str,
) -> FileHashError {
    FileHashError::new(
        code,
        "validate",
        message,
        details([
            ("root", Value::String(root.to_owned())),
            ("reason", Value::String(reason.to_owned())),
        ]),
    )
}

fn root_error_with_io(
    code: &'static str,
    message: &'static str,
    root: &str,
    error: &io::Error,
) -> FileHashError {
    FileHashError::new(
        code,
        "validate",
        message,
        details([
            ("root", Value::String(root.to_owned())),
            ("io_kind", Value::String(format!("{:?}", error.kind()))),
            ("reason", Value::String(error.to_string())),
        ]),
    )
}

fn root_metadata_error(root: &str, error: &io::Error) -> FileHashError {
    if error.kind() == io::ErrorKind::NotFound {
        root_error_with_io(
            "hash_files_root_not_found",
            "hash.files root 不存在",
            root,
            error,
        )
    } else {
        root_error_with_io(
            "hash_files_root_metadata_failed",
            "hash.files 无法读取 root 元数据",
            root,
            error,
        )
    }
}

fn file_io_error(
    code: &'static str,
    message: &'static str,
    input: &ValidatedInput,
    error: &io::Error,
) -> FileHashError {
    FileHashError::new(
        code,
        "validate",
        message,
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("io_kind", Value::String(format!("{:?}", error.kind()))),
            ("reason", Value::String(error.to_string())),
        ]),
    )
}

fn prepared_io_error(
    code: &'static str,
    message: &'static str,
    input: &PreparedInput,
    error: &io::Error,
) -> FileHashError {
    FileHashError::new(
        code,
        "execute",
        message,
        details([
            ("id", Value::String(input.id.clone())),
            ("relative_path", Value::String(input.relative_path.clone())),
            ("io_kind", Value::String(format!("{:?}", error.kind()))),
            ("reason", Value::String(error.to_string())),
        ]),
    )
}

fn details<const N: usize>(values: [(&str, Value); N]) -> BTreeMap<String, Value> {
    values
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::{
        FileIdentity, MetadataSnapshot, PreparedInput, StabilityObservations, hash_files_impl,
        verify_metadata_stability,
    };
    use crate::native_core::pool;
    use serde_json::{Value, json};
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_DIRECTORY_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new() -> Self {
            let sequence = TEST_DIRECTORY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "att_mz_hash_files_{}_{}",
                std::process::id(),
                sequence
            ));
            fs::create_dir_all(&path).expect("应创建测试目录");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn request(root: &Path, files: Value) -> String {
        json!({
            "root": root.to_string_lossy(),
            "files": files,
        })
        .to_string()
    }

    fn digest(text: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(text.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    #[test]
    fn hashes_files_in_request_order_with_sizes() {
        let directory = TestDirectory::new();
        fs::create_dir(directory.path().join("nested")).expect("应创建子目录");
        fs::write(directory.path().join("nested/二.txt"), "第二").expect("应写测试文件");
        fs::write(directory.path().join("first.bin"), b"first\0bytes").expect("应写测试文件");
        let payload = request(
            directory.path(),
            json!([
                {"id": "second", "relative_path": "nested/二.txt"},
                {"id": "first", "relative_path": "first.bin"},
            ]),
        );

        let output: Value = serde_json::from_str(&hash_files_impl(&payload).expect("哈希应成功"))
            .expect("输出应为 JSON");

        assert_eq!(output["files"][0]["id"], "second");
        assert_eq!(output["files"][0]["relative_path"], "nested/二.txt");
        assert_eq!(output["files"][0]["sha256"], digest("第二"));
        assert_eq!(output["files"][0]["byte_size"], "第二".len());
        assert_eq!(output["files"][1]["id"], "first");
        assert_eq!(output["files"][1]["byte_size"], 11);
    }

    #[test]
    fn one_two_and_four_threads_produce_identical_output() {
        let directory = TestDirectory::new();
        for index in 0..12 {
            fs::write(
                directory.path().join(format!("{index:02}.txt")),
                format!("内容-{index}"),
            )
            .expect("应写测试文件");
        }
        let payload = request(
            directory.path(),
            Value::Array(
                (0..12)
                    .map(|index| {
                        json!({
                            "id": format!("F{index:02}"),
                            "relative_path": format!("{index:02}.txt"),
                        })
                    })
                    .collect(),
            ),
        );
        let run = |threads: &str| {
            pool::with_thread_count_override_for_test(Some(threads), || {
                hash_files_impl(&payload).expect("哈希应成功")
            })
        };

        let one = run("1");
        assert_eq!(run("2"), one);
        assert_eq!(run("4"), one);
    }

    #[test]
    fn rejects_unknown_fields_duplicates_and_unsafe_paths() {
        let directory = TestDirectory::new();
        fs::write(directory.path().join("a.txt"), "a").expect("应写测试文件");
        let unknown_payload = json!({
            "root": directory.path().to_string_lossy(),
            "files": [{"id": "a", "relative_path": "a.txt", "extra": true}],
        })
        .to_string();
        assert_eq!(
            hash_files_impl(&unknown_payload)
                .expect_err("未知字段必须失败")
                .code,
            "hash_files_invalid_payload"
        );

        let duplicate_id = request(
            directory.path(),
            json!([
                {"id": "same", "relative_path": "a.txt"},
                {"id": "same", "relative_path": "b.txt"},
            ]),
        );
        assert_eq!(
            hash_files_impl(&duplicate_id)
                .expect_err("重复 id 必须失败")
                .code,
            "hash_files_duplicate_id"
        );

        fs::create_dir(directory.path().join("nested")).expect("应创建目录");
        fs::write(directory.path().join("nested/b.txt"), "b").expect("应写测试文件");
        let duplicate_path = request(
            directory.path(),
            json!([
                {"id": "one", "relative_path": "nested/b.txt"},
                {"id": "two", "relative_path": "nested\\b.txt"},
            ]),
        );
        assert_eq!(
            hash_files_impl(&duplicate_path)
                .expect_err("等价路径必须视为重复")
                .code,
            "hash_files_duplicate_path"
        );

        for relative_path in [
            ".",
            "../a.txt",
            "nested/./b.txt",
            "/absolute.txt",
            "C:drive.txt",
        ] {
            let payload = request(
                directory.path(),
                json!([{"id": "bad", "relative_path": relative_path}]),
            );
            assert_eq!(
                hash_files_impl(&payload)
                    .expect_err("危险路径必须失败")
                    .code,
                "hash_files_invalid_relative_path",
                "未拒绝 {relative_path}"
            );
        }
    }

    #[test]
    fn rejects_missing_files_and_directories() {
        let directory = TestDirectory::new();
        fs::create_dir(directory.path().join("folder")).expect("应创建目录");
        let missing = request(
            directory.path(),
            json!([{"id": "missing", "relative_path": "missing.txt"}]),
        );
        let error = hash_files_impl(&missing).expect_err("缺失文件必须失败");
        assert_eq!(error.code, "hash_files_file_not_found");
        assert_eq!(error.details["id"], "missing");

        let directory_target = request(
            directory.path(),
            json!([{"id": "folder", "relative_path": "folder"}]),
        );
        assert_eq!(
            hash_files_impl(&directory_target)
                .expect_err("目录目标必须失败")
                .code,
            "hash_files_not_regular_file"
        );
    }

    #[test]
    fn accepts_empty_batch_and_rejects_invalid_roots() {
        let directory = TestDirectory::new();
        let empty_output: Value = serde_json::from_str(
            &hash_files_impl(&request(directory.path(), json!([]))).expect("空批次应成功"),
        )
        .expect("输出应为 JSON");
        assert_eq!(empty_output["files"], json!([]));

        let relative_root = json!({"root": "relative", "files": []}).to_string();
        assert_eq!(
            hash_files_impl(&relative_root)
                .expect_err("相对 root 必须失败")
                .code,
            "hash_files_invalid_root"
        );

        let missing_root = directory.path().join("missing");
        assert_eq!(
            hash_files_impl(&request(&missing_root, json!([])))
                .expect_err("缺失 root 必须失败")
                .code,
            "hash_files_root_not_found"
        );

        let file_root = directory.path().join("root.txt");
        fs::write(&file_root, "not a directory").expect("应写测试文件");
        assert_eq!(
            hash_files_impl(&request(&file_root, json!([])))
                .expect_err("文件 root 必须失败")
                .code,
            "hash_files_root_not_directory"
        );
    }

    #[test]
    fn rejects_non_directory_parent_component() {
        let directory = TestDirectory::new();
        fs::write(directory.path().join("parent.txt"), "not a directory").expect("应写测试文件");
        let payload = request(
            directory.path(),
            json!([{"id": "nested", "relative_path": "parent.txt/child.txt"}]),
        );

        assert_eq!(
            hash_files_impl(&payload)
                .expect_err("非目录父级必须失败")
                .code,
            "hash_files_parent_not_directory"
        );
    }

    #[cfg(windows)]
    #[test]
    fn reports_stable_read_error_for_exclusively_locked_file() {
        use std::fs::OpenOptions;
        use std::os::windows::fs::OpenOptionsExt;

        let directory = TestDirectory::new();
        let file_path = directory.path().join("locked.txt");
        fs::write(&file_path, "locked").expect("应写测试文件");
        let _lock = OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(&file_path)
            .expect("应独占打开测试文件");
        let payload = request(
            directory.path(),
            json!([{"id": "locked", "relative_path": "locked.txt"}]),
        );

        let error = hash_files_impl(&payload).expect_err("独占锁必须导致读取失败");
        assert_eq!(error.code, "hash_files_read_failed");
        assert_eq!(error.stage, "execute");
        assert_eq!(error.details["id"], "locked");
        assert!(error.details.contains_key("io_kind"));
    }

    #[test]
    fn rejects_same_sized_path_replacement_by_file_identity() {
        let directory = TestDirectory::new();
        let original_path = directory.path().join("original.txt");
        let replacement_path = directory.path().join("replacement.txt");
        fs::write(&original_path, "AAAA").expect("应写原文件");
        fs::write(&replacement_path, "BBBB").expect("应写替换文件");
        let original = MetadataSnapshot::from_metadata(
            &fs::metadata(&original_path).expect("应读取原文件元数据"),
        );
        let replacement = MetadataSnapshot::from_metadata(
            &fs::metadata(&replacement_path).expect("应读取替换文件元数据"),
        );
        let original_file = fs::File::open(&original_path).expect("应打开原文件");
        let replacement_file = fs::File::open(&replacement_path).expect("应打开替换文件");
        let original_identity = FileIdentity::from_file(&original_file).expect("应读取原文件身份");
        let replacement_identity =
            FileIdentity::from_file(&replacement_file).expect("应读取替换文件身份");
        assert_eq!(original.byte_size, replacement.byte_size);
        assert_ne!(
            original_identity, replacement_identity,
            "同尺寸不同文件必须有不同平台身份"
        );
        let input = PreparedInput {
            id: "race".to_owned(),
            relative_path: "original.txt".to_owned(),
            absolute_path: original_path,
        };

        let observations = StabilityObservations {
            path_before: &original,
            handle_before: &original,
            handle_after: &original,
            path_after: &replacement,
            identity_before: &original_identity,
            identity_after: &original_identity,
            path_after_identity: &replacement_identity,
            read_size: original.byte_size,
        };
        let error =
            verify_metadata_stability(&input, &observations).expect_err("同尺寸路径替换必须失败");

        assert_eq!(error.code, "hash_files_file_changed");
        assert_eq!(error.details["file_identity_changed"], true);
        assert_eq!(error.details["read_size"], 4);
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symbolic_links() {
        use std::os::unix::fs::symlink;

        let directory = TestDirectory::new();
        fs::write(directory.path().join("target.txt"), "target").expect("应写测试文件");
        symlink("target.txt", directory.path().join("link.txt")).expect("应创建符号链接");
        let payload = request(
            directory.path(),
            json!([{"id": "link", "relative_path": "link.txt"}]),
        );

        assert_eq!(
            hash_files_impl(&payload)
                .expect_err("符号链接必须失败")
                .code,
            "hash_files_link_not_allowed"
        );

        fs::create_dir(directory.path().join("target_dir")).expect("应创建目录");
        fs::write(directory.path().join("target_dir/file.txt"), "target").expect("应写测试文件");
        let root_link = directory.path().join("root_link");
        symlink(directory.path().join("target_dir"), &root_link).expect("应创建目录链接");
        assert_eq!(
            hash_files_impl(&request(
                &root_link,
                json!([{"id": "root", "relative_path": "file.txt"}]),
            ))
            .expect_err("链接 root 必须失败")
            .code,
            "hash_files_link_not_allowed"
        );
    }

    #[cfg(windows)]
    #[test]
    fn rejects_windows_file_and_directory_reparse_links_when_available() {
        use std::os::windows::fs::{symlink_dir, symlink_file};

        let directory = TestDirectory::new();
        fs::write(directory.path().join("target.txt"), "target").expect("应写测试文件");
        let file_link = directory.path().join("link.txt");
        if symlink_file(directory.path().join("target.txt"), &file_link).is_ok() {
            let payload = request(
                directory.path(),
                json!([{"id": "link", "relative_path": "link.txt"}]),
            );
            assert_eq!(
                hash_files_impl(&payload)
                    .expect_err("文件符号链接必须失败")
                    .code,
                "hash_files_link_not_allowed"
            );
        }

        fs::create_dir(directory.path().join("target_dir")).expect("应创建目录");
        fs::write(directory.path().join("target_dir/file.txt"), "target").expect("应写测试文件");
        let directory_link = directory.path().join("link_dir");
        if symlink_dir(directory.path().join("target_dir"), &directory_link).is_ok() {
            let payload = request(
                directory.path(),
                json!([{"id": "link", "relative_path": "link_dir/file.txt"}]),
            );
            assert_eq!(
                hash_files_impl(&payload)
                    .expect_err("目录重解析链接必须失败")
                    .code,
                "hash_files_link_not_allowed"
            );
            assert_eq!(
                hash_files_impl(&request(
                    &directory_link,
                    json!([{"id": "root", "relative_path": "file.txt"}]),
                ))
                .expect_err("重解析 root 必须失败")
                .code,
                "hash_files_link_not_allowed"
            );
        }
    }
}
