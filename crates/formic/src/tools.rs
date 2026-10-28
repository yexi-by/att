//! Formic 内置只读工具。这里唯一拥有参数语义、路径边界、结果截断和缓存键规范化。

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::SystemTime;

use cap_std::ambient_authority;
use cap_std::fs::Dir;
use grep_regex::RegexMatcherBuilder;
use grep_searcher::{BinaryDetection, SearcherBuilder, Sink, SinkMatch};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio_util::sync::CancellationToken;

use crate::config::{ReadToolConfig, SearchToolConfig, ToolsConfig};
use crate::image_input::{self, ToolImage};
use crate::llm::{InputModalities, ToolSpec};
use crate::output::RecordFormat;
use crate::output_access::WorkerOutputAccess;

/// input 始终存在；只有明确授权时才持有已发布结果根。
#[derive(Clone)]
pub struct Roots {
    pub input: ReadRoot,
    pub output: Option<ReadRoot>,
    pub output_format: RecordFormat,
}

/// 启动时打开一次的只读目录能力。后续所有文件访问都相对此句柄完成；保存的路径
/// 只用于错误信息和测试准备，不参与文件打开或目录遍历。
#[derive(Clone)]
pub struct ReadRoot {
    dir: Arc<Dir>,
    snapshot: Option<Arc<RootSnapshot>>,
}

struct RootSnapshot {
    files: BTreeMap<PathBuf, FileStamp>,
    digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct FileStamp {
    identity: FileIdentity,
    content_digest: [u8; 32],
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct FileIdentity {
    len: u64,
    modified: Option<SystemTime>,
    created: Option<SystemTime>,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(unix)]
    change_seconds: i64,
    #[cfg(unix)]
    change_nanoseconds: i64,
}

impl ReadRoot {
    /// 使用 ambient authority 打开根目录。这是读取边界唯一一次按环境路径打开根；
    /// 调用方应在启动阶段创建并在整个作业中复用返回值。
    pub fn open(path: impl Into<PathBuf>) -> io::Result<Self> {
        let path = path.into();
        let dir = Dir::open_ambient_dir(&path, ambient_authority())?;
        Ok(Self {
            dir: Arc::new(dir),
            snapshot: None,
        })
    }

    pub(crate) fn from_dir(dir: Dir) -> Self {
        Self {
            dir: Arc::new(dir),
            snapshot: None,
        }
    }

    pub(crate) fn open_file(&self, relative: &Path) -> io::Result<fs::File> {
        let mut file = self.open_live_file(relative)?;
        if let Some(snapshot) = &self.snapshot {
            let expected = snapshot.files.get(relative).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("输入快照不含文件 {}", crate::prompt::slash_path(relative)),
                )
            })?;
            let before = FileIdentity::from_metadata(&file.metadata()?);
            if before != expected.identity {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "输入文件 {} 在作业启动后发生变化",
                        crate::prompt::slash_path(relative)
                    ),
                ));
            }
            let mut content = Sha256::new();
            let mut buffer = [0u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                content.update(&buffer[..read]);
            }
            let actual_digest: [u8; 32] = content.finalize().into();
            let after = FileIdentity::from_metadata(&file.metadata()?);
            if after != expected.identity || actual_digest != expected.content_digest {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "输入文件 {} 与作业快照内容不同",
                        crate::prompt::slash_path(relative)
                    ),
                ));
            }
            file.seek(SeekFrom::Start(0))?;
        }
        Ok(file)
    }

    /// 计划形状校验只确认文件身份；实际读取仍由 `open_file` 按内容摘要验收。
    pub(crate) fn check_file(&self, relative: &Path) -> io::Result<()> {
        let file = self.open_live_file(relative)?;
        if let Some(snapshot) = &self.snapshot {
            let expected = snapshot.files.get(relative).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("输入快照不含文件 {}", crate::prompt::slash_path(relative)),
                )
            })?;
            if FileIdentity::from_metadata(&file.metadata()?) != expected.identity {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "输入文件 {} 在作业启动后发生变化",
                        crate::prompt::slash_path(relative)
                    ),
                ));
            }
        }
        Ok(())
    }

    fn open_live_file(&self, relative: &Path) -> io::Result<fs::File> {
        validate_relative_path(relative)?;
        self.reject_link_components(relative)?;
        let file = self.dir.open(relative)?.into_std();
        if !file.metadata()?.is_file() {
            return Err(io::Error::other(format!(
                "路径 {} 不是文件",
                crate::prompt::slash_path(relative)
            )));
        }
        Ok(file)
    }

    /// 冻结本轮执行能够观察到的输入文件集合和元数据，并以相同文件内容生成作业摘要。
    /// 后续遍历只返回这份集合，打开文件时再次核对身份、大小和时间戳。
    pub(crate) fn freeze(&self) -> io::Result<(Self, String)> {
        if let Some(snapshot) = &self.snapshot {
            return Ok((self.clone(), snapshot.digest.clone()));
        }
        let paths = walk_files_live(self, None)?;
        let mut files = BTreeMap::new();
        let mut aggregate = Sha256::new();
        for relative in &paths {
            let mut file = self.open_live_file(relative)?;
            let before = FileIdentity::from_metadata(&file.metadata()?);
            let mut content = Sha256::new();
            let mut content_len = 0u64;
            let mut buffer = [0u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                content_len = content_len
                    .checked_add(read as u64)
                    .ok_or_else(|| io::Error::other("输入文件长度溢出"))?;
                content.update(&buffer[..read]);
            }
            let after = FileIdentity::from_metadata(&file.metadata()?);
            if before != after || before.len != content_len {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "建立输入快照时文件 {} 发生变化",
                        crate::prompt::slash_path(relative)
                    ),
                ));
            }
            let path_bytes = path_identity_bytes(relative);
            aggregate.update((path_bytes.len() as u64).to_le_bytes());
            aggregate.update(path_bytes);
            aggregate.update(content_len.to_le_bytes());
            let content_digest: [u8; 32] = content.finalize().into();
            aggregate.update(content_digest);
            files.insert(
                relative.clone(),
                FileStamp {
                    identity: before,
                    content_digest,
                },
            );
        }

        // 捕获期间新增、删除、替换或改写的文件不能形成混合版本摘要。
        let after_paths = walk_files_live(self, None)?;
        if paths != after_paths {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "建立输入快照时文件集合发生变化",
            ));
        }
        for relative in &after_paths {
            let file = self.open_live_file(relative)?;
            let actual = FileIdentity::from_metadata(&file.metadata()?);
            if files.get(relative).map(|stamp| &stamp.identity) != Some(&actual) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "建立输入快照时文件 {} 发生变化",
                        crate::prompt::slash_path(relative)
                    ),
                ));
            }
        }

        let snapshot = Arc::new(RootSnapshot {
            files,
            digest: format!("{:x}", aggregate.finalize()),
        });
        Ok((
            Self {
                dir: Arc::clone(&self.dir),
                snapshot: Some(Arc::clone(&snapshot)),
            },
            snapshot.digest.clone(),
        ))
    }

    /// 静态目录树中的链接仍按产品契约拒绝；路径被并发替换时，根句柄相对打开
    /// 继续保证结果无法逃逸 capability 根。
    fn reject_link_components(&self, relative: &Path) -> io::Result<()> {
        let mut current = PathBuf::new();
        for component in relative.components() {
            current.push(component.as_os_str());
            let metadata = self.dir.symlink_metadata(&current).map_err(|error| {
                io::Error::new(
                    error.kind(),
                    format!(
                        "路径 {} 不可用：{error}",
                        crate::prompt::slash_path(relative)
                    ),
                )
            })?;
            if metadata.file_type().is_symlink() {
                return Err(io::Error::other(format!(
                    "路径 {} 包含符号链接或目录联接",
                    crate::prompt::slash_path(relative)
                )));
            }
        }
        Ok(())
    }

    pub(crate) fn clone_dir(&self) -> io::Result<Dir> {
        self.dir.try_clone()
    }

    fn open_dir(&self, relative: &Path) -> io::Result<Dir> {
        if relative.as_os_str().is_empty() {
            return self.clone_dir();
        }
        validate_relative_path(relative)?;
        self.reject_link_components(relative)?;
        self.dir.open_dir(relative)
    }
}

impl FileIdentity {
    fn from_metadata(metadata: &fs::Metadata) -> Self {
        #[cfg(unix)]
        use std::os::unix::fs::MetadataExt as _;
        Self {
            len: metadata.len(),
            modified: metadata.modified().ok(),
            created: metadata.created().ok(),
            #[cfg(unix)]
            device: metadata.dev(),
            #[cfg(unix)]
            inode: metadata.ino(),
            #[cfg(unix)]
            change_seconds: metadata.ctime(),
            #[cfg(unix)]
            change_nanoseconds: metadata.ctime_nsec(),
        }
    }
}

#[cfg(unix)]
fn path_identity_bytes(path: &Path) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt as _;
    path.as_os_str().as_bytes().to_vec()
}

#[cfg(windows)]
fn path_identity_bytes(path: &Path) -> Vec<u8> {
    use std::os::windows::ffi::OsStrExt as _;
    path.as_os_str()
        .encode_wide()
        .flat_map(u16::to_le_bytes)
        .collect()
}

#[cfg(not(any(unix, windows)))]
fn path_identity_bytes(path: &Path) -> Vec<u8> {
    path.as_os_str().to_string_lossy().as_bytes().to_vec()
}

#[derive(Debug, Clone)]
pub enum BuiltinTool {
    Search(SearchToolConfig),
    Read(ReadToolConfig),
    ReadImage,
}

pub struct PreparedBuiltinCall {
    operation: BuiltinOperation,
}

enum BuiltinOperation {
    Search(SearchToolConfig, SearchArgs),
    Read(ReadToolConfig, ReadArgs),
    ReadImage(PathBuf),
}

pub struct BuiltinRegistration {
    pub spec: ToolSpec,
    pub executor: BuiltinTool,
    pub max_in_flight: usize,
}

#[derive(Debug, Clone)]
pub struct ToolOutput {
    pub content: String,
    pub images: Vec<ToolImage>,
    pub cacheable: bool,
}

impl ToolOutput {
    fn success(content: String, truncated: bool, cacheable: bool) -> Self {
        Self {
            content,
            images: Vec::new(),
            cacheable: cacheable && !truncated,
        }
    }

    pub fn error(message: impl std::fmt::Display) -> Self {
        Self {
            content: format!("错误：{message}"),
            images: Vec::new(),
            cacheable: false,
        }
    }

    fn image(path: PathBuf, data: image_input::ImageData) -> Self {
        Self {
            content: format!(
                "已读取图片 {}（{}，{}×{}）",
                crate::prompt::slash_path(&path),
                data.media_type().mime(),
                data.width(),
                data.height()
            ),
            images: vec![ToolImage::Input { path, data }],
            cacheable: false,
        }
    }
}

pub fn registrations(
    config: &ToolsConfig,
    output_access: WorkerOutputAccess,
    input_modalities: InputModalities,
) -> Vec<BuiltinRegistration> {
    let mut registrations = Vec::new();
    if config.read.enabled {
        registrations.push(BuiltinRegistration {
            spec: read_spec(output_access),
            executor: BuiltinTool::Read(config.read.clone()),
            max_in_flight: config.read.max_in_flight,
        });
    }
    if config.search.enabled {
        registrations.push(BuiltinRegistration {
            spec: search_spec(config.search.max_context_lines, output_access),
            executor: BuiltinTool::Search(config.search.clone()),
            max_in_flight: config.search.max_in_flight,
        });
    }
    if input_modalities.supports_image() {
        registrations.push(BuiltinRegistration {
            spec: read_image_spec(),
            executor: BuiltinTool::ReadImage,
            max_in_flight: config.max_in_flight,
        });
    }
    registrations.sort_by(|left, right| left.spec.name.cmp(&right.spec.name));
    registrations
}

fn read_image_spec() -> ToolSpec {
    ToolSpec {
        name: "read_image".into(),
        description: "读取冻结 input 根中的 JPEG、PNG、GIF 或 WebP 图片。".into(),
        parameters: serde_json::json!({
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "input 根内相对路径"}
            },
            "required": ["path"],
            "additionalProperties": false
        }),
    }
}

fn search_spec(max_context_lines: usize, output_access: WorkerOutputAccess) -> ToolSpec {
    let scopes = if output_access.allows_published() {
        serde_json::json!(["input", "output"])
    } else {
        serde_json::json!(["input"])
    };
    let description = if output_access.allows_published() {
        "在只读 input 数据集或已发布结果中搜索文本；结果截断时有明确标记。"
    } else {
        "在只读 input 数据集中搜索文本；结果截断时有明确标记。"
    };
    ToolSpec {
        name: "search".into(),
        description: description.into(),
        parameters: serde_json::json!({
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式；literal 为 true 时按字面量匹配"},
                "scope": {"type": "string", "enum": scopes},
                "glob": {"type": "string", "description": "可选，根内 glob 过滤，如 **/*.txt"},
                "context": {"type": "integer", "minimum": 0, "maximum": max_context_lines},
                "literal": {"type": "boolean"}
            },
            "required": ["pattern", "scope"],
            "additionalProperties": false
        }),
    }
}

fn read_spec(output_access: WorkerOutputAccess) -> ToolSpec {
    let scopes = if output_access.allows_published() {
        serde_json::json!(["input", "output"])
    } else {
        serde_json::json!(["input"])
    };
    let description = if output_access.allows_published() {
        "按根内相对路径读取 UTF-8 文本；行号从 1 开始，区间为闭区间。output 只允许数字编号完成记录。"
    } else {
        "按 input 根内相对路径读取 UTF-8 文本；行号从 1 开始，区间为闭区间。"
    };
    ToolSpec {
        name: "read".into(),
        description: description.into(),
        parameters: serde_json::json!({
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": scopes},
                "path": {"type": "string", "description": "根内相对路径"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1}
            },
            "required": ["scope", "path"],
            "additionalProperties": false
        }),
    }
}

impl BuiltinTool {
    pub fn prepare(&self, arguments: &Value) -> Result<PreparedBuiltinCall, String> {
        let operation = match self {
            Self::Search(config) => {
                BuiltinOperation::Search(config.clone(), parse_search(arguments, config)?)
            }
            Self::Read(config) => BuiltinOperation::Read(config.clone(), parse_read(arguments)?),
            Self::ReadImage => BuiltinOperation::ReadImage(parse_read_image(arguments)?),
        };
        Ok(PreparedBuiltinCall { operation })
    }

    #[cfg(test)]
    pub fn execute(&self, roots: &Roots, arguments: &Value) -> ToolOutput {
        match self.prepare(arguments) {
            Ok(call) => call.execute_cancellable(roots, &CancellationToken::new()),
            Err(message) => ToolOutput::error(message),
        }
    }
}

impl PreparedBuiltinCall {
    pub fn canonical_cache_key(&self) -> Option<String> {
        let arguments = match &self.operation {
            BuiltinOperation::Search(_, args) if args.scope == Scope::Input => args.canonical(),
            BuiltinOperation::Read(_, args) if args.scope == Scope::Input => args.canonical(),
            _ => return None,
        };
        Some(serde_json::to_string(&arguments).expect("规范参数可序列化"))
    }

    pub fn is_search(&self) -> bool {
        matches!(self.operation, BuiltinOperation::Search(..))
    }

    pub fn execute_cancellable(self, roots: &Roots, cancel: &CancellationToken) -> ToolOutput {
        match self.operation {
            BuiltinOperation::Search(config, args) => {
                search(roots, &config, &args, cancel).unwrap_or_else(ToolOutput::error)
            }
            BuiltinOperation::Read(config, args) => {
                read(roots, &config, &args, cancel).unwrap_or_else(ToolOutput::error)
            }
            BuiltinOperation::ReadImage(path) => {
                match image_input::read_input_image(&roots.input, &path, cancel) {
                    Ok(data) => ToolOutput::image(path, data),
                    Err(message) => ToolOutput::error(message),
                }
            }
        }
    }
}

fn parse_read_image(arguments: &Value) -> Result<PathBuf, String> {
    let object = arguments
        .as_object()
        .ok_or_else(|| "工具参数必须是 JSON object".to_string())?;
    reject_unknown(object, &["path"])?;
    let path = object
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| "缺少字符串参数 path".to_string())?;
    if path
        .get(..7)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("http://"))
        || path
            .get(..8)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("https://"))
    {
        return Err("path 只接受 input 根相对路径，不支持 HTTP URL".into());
    }
    safe_relative_path(path)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scope {
    Input,
    Output,
}

impl Scope {
    fn parse(value: &Value) -> Result<Self, String> {
        match value.as_str() {
            Some("input") => Ok(Self::Input),
            Some("output") => Ok(Self::Output),
            _ => Err("scope 必须是 input 或 output".into()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Input => "input",
            Self::Output => "output",
        }
    }
}

struct SearchArgs {
    pattern: String,
    scope: Scope,
    glob: Option<String>,
    context: usize,
    literal: bool,
}

impl SearchArgs {
    fn canonical(&self) -> Value {
        serde_json::json!({
            "context": self.context,
            "glob": self.glob,
            "literal": self.literal,
            "pattern": self.pattern,
            "scope": self.scope.name(),
        })
    }
}

fn parse_search(arguments: &Value, config: &SearchToolConfig) -> Result<SearchArgs, String> {
    let object = arguments
        .as_object()
        .ok_or_else(|| "工具参数必须是 JSON object".to_string())?;
    reject_unknown(object, &["pattern", "scope", "glob", "context", "literal"])?;
    let pattern = object
        .get("pattern")
        .and_then(Value::as_str)
        .ok_or_else(|| "缺少字符串参数 pattern".to_string())?;
    let scope = Scope::parse(object.get("scope").unwrap_or(&Value::Null))?;
    let glob = object
        .get("glob")
        .map(|value| {
            value
                .as_str()
                .map(str::to_string)
                .ok_or_else(|| "glob 必须是字符串".to_string())
        })
        .transpose()?;
    let context = object
        .get("context")
        .map(Value::as_u64)
        .transpose_option("context 必须是非负整数")?
        .unwrap_or(0) as usize;
    if context > config.max_context_lines {
        return Err(format!(
            "context 不能超过配置的 {} 行",
            config.max_context_lines
        ));
    }
    let literal = object
        .get("literal")
        .map(|value| {
            value
                .as_bool()
                .ok_or_else(|| "literal 必须是布尔值".to_string())
        })
        .transpose()?
        .unwrap_or(false);
    Ok(SearchArgs {
        pattern: pattern.to_string(),
        scope,
        glob,
        context,
        literal,
    })
}

struct ReadArgs {
    scope: Scope,
    path: PathBuf,
    start_line: u64,
    end_line: Option<u64>,
}

impl ReadArgs {
    fn canonical(&self) -> Value {
        serde_json::json!({
            "end_line": self.end_line,
            "path": crate::prompt::slash_path(&self.path),
            "scope": self.scope.name(),
            "start_line": self.start_line,
        })
    }
}

fn parse_read(arguments: &Value) -> Result<ReadArgs, String> {
    let object = arguments
        .as_object()
        .ok_or_else(|| "工具参数必须是 JSON object".to_string())?;
    reject_unknown(object, &["scope", "path", "start_line", "end_line"])?;
    let scope = Scope::parse(object.get("scope").unwrap_or(&Value::Null))?;
    let raw_path = object
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| "缺少字符串参数 path".to_string())?;
    let path = safe_relative_path(raw_path)?;
    let start_line = positive_integer(object.get("start_line"), "start_line")?.unwrap_or(1);
    let end_line = positive_integer(object.get("end_line"), "end_line")?;
    if end_line.is_some_and(|end| end < start_line) {
        return Err("end_line 不能小于 start_line".into());
    }
    Ok(ReadArgs {
        scope,
        path,
        start_line,
        end_line,
    })
}

trait OptionalU64 {
    fn transpose_option(self, message: &str) -> Result<Option<u64>, String>;
}

impl OptionalU64 for Option<Option<u64>> {
    fn transpose_option(self, message: &str) -> Result<Option<u64>, String> {
        match self {
            Some(Some(value)) => Ok(Some(value)),
            Some(None) => Err(message.into()),
            None => Ok(None),
        }
    }
}

fn positive_integer(value: Option<&Value>, name: &str) -> Result<Option<u64>, String> {
    value
        .map(|value| {
            value
                .as_u64()
                .filter(|value| *value > 0)
                .ok_or_else(|| format!("{name} 必须是正整数"))
        })
        .transpose()
}

fn reject_unknown(object: &serde_json::Map<String, Value>, allowed: &[&str]) -> Result<(), String> {
    if let Some(name) = object.keys().find(|name| !allowed.contains(&name.as_str())) {
        return Err(format!("未知参数 {name}"));
    }
    Ok(())
}

fn safe_relative_path(raw: &str) -> Result<PathBuf, String> {
    if raw.is_empty() {
        return Err("path 不能为空".into());
    }
    let path = Path::new(raw);
    if path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err("path 必须是根内相对路径，且不能包含 . 或 ..".into());
    }
    Ok(path.to_path_buf())
}

fn search(
    roots: &Roots,
    config: &SearchToolConfig,
    args: &SearchArgs,
    cancel: &CancellationToken,
) -> Result<ToolOutput, String> {
    let mut builder = RegexMatcherBuilder::new();
    if args.literal {
        builder.fixed_strings(true);
    }
    let matcher = builder
        .build(&args.pattern)
        .map_err(|error| format!("pattern 不是合法的模式：{error}"))?;
    let glob_matcher = args
        .glob
        .as_deref()
        .map(|glob| {
            globset::GlobBuilder::new(glob)
                .build()
                .map(|glob| glob.compile_matcher())
                .map_err(|error| format!("glob 不合法：{error}"))
        })
        .transpose()?;
    let root = scope_root(roots, args.scope)?;
    let files = scope_files(root, args.scope, roots.output_format, Some(cancel))
        .map_err(|error| format!("无法遍历根目录：{error}"))?;

    let mut out = String::new();
    let mut matches = 0usize;
    let mut truncated = None;
    'files: for relative in &files {
        if cancel.is_cancelled() {
            return Err("工具调用已取消".into());
        }
        if glob_matcher
            .as_ref()
            .is_some_and(|matcher| !matcher.is_match(relative))
        {
            continue;
        }
        let file = open_root_file(root, relative).map_err(|error| {
            format!("无法读取 {}：{error}", crate::prompt::slash_path(relative))
        })?;
        let mut sink = MatchSink {
            path: crate::prompt::slash_path(relative),
            context: args.context,
            max_matches: config.max_matches,
            max_bytes: config.max_result_bytes,
            header_written: false,
            out: &mut out,
            matches: &mut matches,
            truncated: &mut truncated,
            cancel,
        };
        let mut searcher = SearcherBuilder::new()
            .line_number(true)
            .before_context(args.context)
            .after_context(args.context)
            .heap_limit(Some(config.max_result_bytes.max(64 * 1024)))
            .binary_detection(BinaryDetection::quit(b'\0'))
            .build();
        let reader = CancellableReader {
            inner: file,
            cancel,
        };
        searcher
            .search_reader(&matcher, reader, &mut sink)
            .map_err(|error| {
                if cancel.is_cancelled() {
                    "工具调用已取消".to_string()
                } else {
                    format!(
                        "搜索 {} 失败：{error}；文件单行不得超过搜索内存上限 {} 字节",
                        crate::prompt::slash_path(relative),
                        config.max_result_bytes.max(64 * 1024)
                    )
                }
            })?;
        if truncated.is_some() {
            break 'files;
        }
    }

    if cancel.is_cancelled() {
        return Err("工具调用已取消".into());
    }

    if matches == 0 && truncated.is_none() {
        return Ok(ToolOutput::success(
            "无匹配。".into(),
            false,
            args.scope == Scope::Input,
        ));
    }
    if let Some(reason) = &truncated {
        apply_truncation_marker(&mut out, reason, config.max_result_bytes);
    }
    Ok(ToolOutput::success(
        out,
        truncated.is_some(),
        args.scope == Scope::Input,
    ))
}

fn read(
    roots: &Roots,
    config: &ReadToolConfig,
    args: &ReadArgs,
    cancel: &CancellationToken,
) -> Result<ToolOutput, String> {
    if args.scope == Scope::Output {
        validate_output_record(&args.path, roots.output_format)?;
    }
    let root = scope_root(roots, args.scope)?;
    let result = read_utf8_lines(
        root,
        &args.path,
        args.start_line,
        args.end_line,
        LineRender::Numbered,
        Some(config.max_result_bytes),
        cancel,
    )
    .map_err(|error| format_read_error(&args.path, error))?;
    let mut out = result.content;
    let truncated = result.truncated;
    if out.is_empty() && !truncated {
        out.push_str("无内容。");
    }
    if truncated {
        apply_truncation_marker(
            &mut out,
            &format!("结果达到 {} 字节上限", config.max_result_bytes),
            config.max_result_bytes,
        );
    }
    Ok(ToolOutput::success(
        out,
        truncated,
        args.scope == Scope::Input,
    ))
}

fn scope_root(roots: &Roots, scope: Scope) -> Result<&ReadRoot, String> {
    match scope {
        Scope::Input => Ok(&roots.input),
        Scope::Output => roots
            .output
            .as_ref()
            .ok_or_else(|| "当前 worker 无权读取已发布结果".into()),
    }
}

fn validate_output_record(path: &Path, format: RecordFormat) -> Result<(), String> {
    if path.components().count() != 1
        || path.extension().and_then(|value| value.to_str()) != Some(format.extension())
        || path
            .file_stem()
            .and_then(|value| value.to_str())
            .and_then(|value| value.parse::<u64>().ok())
            .is_none_or(|unit| unit == 0)
    {
        return Err(format!(
            "output 只允许读取顶层数字编号的 .{} 完成记录",
            format.extension()
        ));
    }
    Ok(())
}

fn validate_relative_path(relative: &Path) -> io::Result<()> {
    if relative.is_absolute()
        || relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(io::Error::other("路径必须是根内不含 . 或 .. 的相对路径"));
    }
    Ok(())
}

/// 通过启动时打开的根 capability 打开文件。安全边界来自目录句柄相对解析，
/// 不依赖可被并发替换的 ambient 路径复查。
pub(crate) fn open_root_file(root: &ReadRoot, relative: &Path) -> io::Result<fs::File> {
    root.open_file(relative)
}

struct CancellableReader<'a> {
    inner: fs::File,
    cancel: &'a CancellationToken,
}

impl Read for CancellableReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.cancel.is_cancelled() {
            // grep-searcher 会自动重试 Interrupted；使用 Other 才能让取消立即终止搜索。
            return Err(io::Error::other("工具调用已取消"));
        }
        self.inner.read(buffer)
    }
}

#[derive(Default)]
struct Utf8StreamDecoder {
    pending: Vec<u8>,
}

impl Utf8StreamDecoder {
    /// 返回 false 表示调用方已经取得足够内容，读取应立即停止。
    fn push(&mut self, bytes: &[u8], mut emit: impl FnMut(&str) -> bool) -> io::Result<bool> {
        if bytes.is_empty() {
            return Ok(true);
        }
        if self.pending.is_empty() {
            return match std::str::from_utf8(bytes) {
                Ok(text) => Ok(emit(text)),
                Err(error) => {
                    let valid = error.valid_up_to();
                    if valid > 0
                        && !emit(std::str::from_utf8(&bytes[..valid]).expect("已确认 UTF-8 前缀"))
                    {
                        return Ok(false);
                    }
                    if error.error_len().is_some() {
                        Err(io::Error::new(io::ErrorKind::InvalidData, "不是合法 UTF-8"))
                    } else {
                        self.pending.extend_from_slice(&bytes[valid..]);
                        Ok(true)
                    }
                }
            };
        }
        let mut combined = Vec::with_capacity(self.pending.len() + bytes.len());
        combined.append(&mut self.pending);
        combined.extend_from_slice(bytes);
        match std::str::from_utf8(&combined) {
            Ok(text) => return Ok(emit(text)),
            Err(error) => {
                let valid = error.valid_up_to();
                if valid > 0
                    && !emit(std::str::from_utf8(&combined[..valid]).expect("已确认 UTF-8 前缀"))
                {
                    return Ok(false);
                }
                if error.error_len().is_some() {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "不是合法 UTF-8"));
                }
                self.pending.extend_from_slice(&combined[valid..]);
            }
        }
        Ok(true)
    }

    fn finish(&self) -> io::Result<()> {
        if self.pending.is_empty() {
            Ok(())
        } else {
            Err(io::Error::new(io::ErrorKind::InvalidData, "不是合法 UTF-8"))
        }
    }
}

/// 逐块解码 UTF-8 文件。emit 返回 false 时不再读取剩余内容。
pub(crate) fn stream_utf8_file(
    root: &ReadRoot,
    relative: &Path,
    cancel: &CancellationToken,
    mut emit: impl FnMut(&str) -> bool,
) -> io::Result<bool> {
    let mut file = open_root_file(root, relative)?;
    let mut decoder = Utf8StreamDecoder::default();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        if cancel.is_cancelled() {
            return Err(io::Error::new(io::ErrorKind::Interrupted, "读取已取消"));
        }
        let count = file.read(&mut buffer)?;
        if count == 0 {
            decoder.finish()?;
            return Ok(true);
        }
        if !decoder.push(&buffer[..count], &mut emit)? {
            return Ok(false);
        }
    }
}

pub(crate) enum LineRender {
    Plain,
    Numbered,
}

pub(crate) struct StreamedText {
    pub content: String,
    pub truncated: bool,
}

/// 流式读取闭区间行范围。只保留选中内容；到达 end 或字节上限后立即停止。
pub(crate) fn read_utf8_lines(
    root: &ReadRoot,
    relative: &Path,
    start: u64,
    end: Option<u64>,
    render: LineRender,
    max_bytes: Option<usize>,
    cancel: &CancellationToken,
) -> io::Result<StreamedText> {
    let mut output = String::new();
    let mut truncated = false;
    stream_utf8_lines(root, relative, start, end, render, cancel, |text| {
        if let Some(limit) = max_bytes {
            let remaining = limit.saturating_sub(output.len());
            if text.len() > remaining {
                output.push_str(truncate_utf8(text, remaining));
                truncated = true;
                return false;
            }
        }
        output.push_str(text);
        true
    })?;
    Ok(StreamedText {
        content: output,
        truncated,
    })
}

/// 流式读取闭区间行范围。emit 返回 false 时立即停止，不读取剩余内容。
pub(crate) fn stream_utf8_lines(
    root: &ReadRoot,
    relative: &Path,
    start: u64,
    end: Option<u64>,
    render: LineRender,
    cancel: &CancellationToken,
    mut emit: impl FnMut(&str) -> bool,
) -> io::Result<bool> {
    let mut file = open_root_file(root, relative)?;
    let mut buffer = [0u8; 64 * 1024];
    let mut decoder = Utf8StreamDecoder::default();
    let mut line_number = 1u64;
    let mut line_has_bytes = false;
    let mut line_started = false;
    let mut selected_any = false;
    let mut held_cr = false;

    'read: loop {
        if cancel.is_cancelled() {
            return Err(io::Error::new(io::ErrorKind::Interrupted, "读取已取消"));
        }
        let count = file.read(&mut buffer)?;
        if count == 0 {
            if line_has_bytes {
                if !begin_selected_line(
                    line_number,
                    start,
                    end,
                    &render,
                    &mut selected_any,
                    &mut line_started,
                    &mut emit,
                ) {
                    return Ok(false);
                }
                if held_cr {
                    let selected =
                        line_number >= start && end.is_none_or(|limit| line_number <= limit);
                    if !decoder.push(b"\r", |text| !selected || emit(text))? {
                        return Ok(false);
                    }
                }
                decoder.finish()?;
                if !finish_selected_line(line_number, start, end, &render, &mut emit) {
                    return Ok(false);
                }
            } else {
                decoder.finish()?;
            }
            break;
        }

        let mut offset = 0usize;
        while offset < count {
            if cancel.is_cancelled() {
                return Err(io::Error::new(io::ErrorKind::Interrupted, "读取已取消"));
            }
            let rest = &buffer[offset..count];
            let newline = rest.iter().position(|byte| *byte == b'\n');
            let length = newline.unwrap_or(rest.len());
            let segment = &rest[..length];
            line_has_bytes |= !segment.is_empty();
            if !begin_selected_line(
                line_number,
                start,
                end,
                &render,
                &mut selected_any,
                &mut line_started,
                &mut emit,
            ) {
                return Ok(false);
            }

            if held_cr && !segment.is_empty() {
                let selected = line_number >= start && end.is_none_or(|limit| line_number <= limit);
                if !decoder.push(b"\r", |text| !selected || emit(text))? {
                    return Ok(false);
                }
            }
            let (content, ends_with_cr) = segment
                .strip_suffix(b"\r")
                .map_or((segment, false), |content| (content, true));
            let selected = line_number >= start && end.is_none_or(|limit| line_number <= limit);
            if !decoder.push(content, |text| !selected || emit(text))? {
                return Ok(false);
            }
            held_cr = ends_with_cr;

            if newline.is_some() {
                decoder.finish()?;
                decoder = Utf8StreamDecoder::default();
                held_cr = false;
                if !finish_selected_line(line_number, start, end, &render, &mut emit) {
                    return Ok(false);
                }
                if end.is_some_and(|limit| line_number >= limit) {
                    break 'read;
                }
                line_number = line_number.saturating_add(1);
                line_has_bytes = false;
                line_started = false;
                offset += length + 1;
            } else {
                offset = count;
            }
        }
    }

    Ok(true)
}

fn begin_selected_line(
    line_number: u64,
    start: u64,
    end: Option<u64>,
    render: &LineRender,
    selected_any: &mut bool,
    line_started: &mut bool,
    emit: &mut impl FnMut(&str) -> bool,
) -> bool {
    if *line_started || line_number < start || end.is_some_and(|limit| line_number > limit) {
        return true;
    }
    *line_started = true;
    let keep_reading = match render {
        LineRender::Plain => {
            if *selected_any {
                emit("\n")
            } else {
                true
            }
        }
        LineRender::Numbered => emit(&format!("{line_number}: ")),
    };
    *selected_any = true;
    keep_reading
}

fn finish_selected_line(
    line_number: u64,
    start: u64,
    end: Option<u64>,
    render: &LineRender,
    emit: &mut impl FnMut(&str) -> bool,
) -> bool {
    if matches!(render, LineRender::Numbered)
        && line_number >= start
        && end.is_none_or(|limit| line_number <= limit)
    {
        emit("\n")
    } else {
        true
    }
}

pub(crate) fn count_utf8_lines(root: &ReadRoot, relative: &Path) -> io::Result<u64> {
    let mut file = open_root_file(root, relative)?;
    let mut decoder = Utf8StreamDecoder::default();
    let mut buffer = [0u8; 64 * 1024];
    let mut newlines = 0u64;
    let mut saw_bytes = false;
    let mut ended_with_newline = false;
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            decoder.finish()?;
            return Ok(newlines + u64::from(saw_bytes && !ended_with_newline));
        }
        saw_bytes = true;
        ended_with_newline = buffer[count - 1] == b'\n';
        newlines = newlines.saturating_add(
            u64::try_from(
                buffer[..count]
                    .iter()
                    .filter(|byte| **byte == b'\n')
                    .count(),
            )
            .unwrap_or(u64::MAX),
        );
        decoder.push(&buffer[..count], |_| true)?;
    }
}

fn format_read_error(path: &Path, error: io::Error) -> String {
    if error.kind() == io::ErrorKind::Interrupted {
        "工具调用已取消".into()
    } else if error.kind() == io::ErrorKind::InvalidData {
        format!("{} 不是合法 UTF-8", crate::prompt::slash_path(path))
    } else {
        format!("读取 {} 失败：{error}", crate::prompt::slash_path(path))
    }
}

fn truncate_utf8(text: &str, max_bytes: usize) -> &str {
    let mut end = max_bytes.min(text.len());
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

fn apply_truncation_marker(output: &mut String, reason: &str, max_bytes: usize) {
    let full = format!("\n[已截断：{reason}]\n");
    let marker = if full.len() <= max_bytes {
        full.as_str()
    } else if max_bytes >= "[cut]".len() {
        "[cut]"
    } else {
        "!"
    };
    let keep = max_bytes.saturating_sub(marker.len()).min(output.len());
    let keep = truncate_utf8(output, keep).len();
    output.truncate(keep);
    output.push_str(marker);
}

fn scope_files(
    root: &ReadRoot,
    scope: Scope,
    format: RecordFormat,
    cancel: Option<&CancellationToken>,
) -> io::Result<Vec<PathBuf>> {
    match scope {
        Scope::Input => walk_files_cancellable(root, cancel),
        Scope::Output => {
            let mut files = Vec::new();
            for entry in root.clone_dir()?.entries()? {
                if cancel.is_some_and(CancellationToken::is_cancelled) {
                    break;
                }
                let entry = entry?;
                let kind = entry.file_type()?;
                if kind.is_symlink() {
                    continue;
                }
                let path = PathBuf::from(entry.file_name());
                let Some(name) = path.file_stem().and_then(|value| value.to_str()) else {
                    continue;
                };
                if kind.is_file()
                    && path.extension().and_then(|value| value.to_str()) == Some(format.extension())
                    && name.parse::<u64>().is_ok_and(|unit| unit > 0)
                {
                    files.push(path);
                }
            }
            files.sort();
            Ok(files)
        }
    }
}

/// 递归列出根内普通文件；符号链接不跟随。
#[cfg(test)]
pub fn walk_files(root: &ReadRoot) -> io::Result<Vec<PathBuf>> {
    walk_files_cancellable(root, None)
}

fn walk_files_cancellable(
    root: &ReadRoot,
    cancel: Option<&CancellationToken>,
) -> io::Result<Vec<PathBuf>> {
    if let Some(snapshot) = &root.snapshot {
        let mut files = Vec::with_capacity(snapshot.files.len());
        for relative in snapshot.files.keys() {
            if cancel.is_some_and(CancellationToken::is_cancelled) {
                break;
            }
            files.push(relative.clone());
        }
        return Ok(files);
    }
    walk_files_live(root, cancel)
}

fn walk_files_live(
    root: &ReadRoot,
    cancel: Option<&CancellationToken>,
) -> io::Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    // 栈只保留相对路径；宽目录或深目录不会同时占用大量句柄。
    let mut stack = vec![PathBuf::new()];
    while let Some(relative_dir) = stack.pop() {
        let directory = root.open_dir(&relative_dir)?;
        for entry in directory.entries()? {
            if cancel.is_some_and(CancellationToken::is_cancelled) {
                return Ok(files);
            }
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_symlink() {
                continue;
            }
            let path = relative_dir.join(entry.file_name());
            if kind.is_dir() {
                stack.push(path);
            } else if kind.is_file() {
                files.push(path);
            }
        }
    }
    files.sort();
    Ok(files)
}

struct MatchSink<'a> {
    path: String,
    context: usize,
    max_matches: usize,
    max_bytes: usize,
    header_written: bool,
    out: &'a mut String,
    matches: &'a mut usize,
    truncated: &'a mut Option<String>,
    cancel: &'a CancellationToken,
}

impl MatchSink<'_> {
    fn push_line(&mut self, line: u64, text: &str, matched: bool) -> bool {
        if self.cancel.is_cancelled() || self.truncated.is_some() {
            return false;
        }
        if matched {
            *self.matches += 1;
            if *self.matches > self.max_matches {
                *self.truncated = Some(format!("匹配数达到 {} 上限", self.max_matches));
                return false;
            }
        }
        let separator = if matched { ':' } else { '-' };
        let line_text = format!("{line}{separator} {text}\n");
        let header_bytes = if self.header_written {
            0
        } else {
            self.path.len() + 7
        };
        if self.out.len() + header_bytes + line_text.len() > self.max_bytes {
            *self.truncated = Some(format!("结果达到 {} 字节上限", self.max_bytes));
            return false;
        }
        if !self.header_written {
            self.out.push_str(&format!("== {} ==\n", self.path));
            self.header_written = true;
        }
        self.out.push_str(&line_text);
        true
    }
}

impl Sink for MatchSink<'_> {
    type Error = io::Error;

    fn matched(
        &mut self,
        _searcher: &grep_searcher::Searcher,
        matched: &SinkMatch,
    ) -> Result<bool, Self::Error> {
        Ok(self.push_line(
            matched.line_number().unwrap_or(0),
            String::from_utf8_lossy(matched.bytes()).trim_end_matches(['\n', '\r']),
            true,
        ))
    }

    fn context(
        &mut self,
        _searcher: &grep_searcher::Searcher,
        context: &grep_searcher::SinkContext,
    ) -> Result<bool, Self::Error> {
        Ok(self.push_line(
            context.line_number().unwrap_or(0),
            String::from_utf8_lossy(context.bytes()).trim_end_matches(['\n', '\r']),
            false,
        ))
    }

    fn context_break(&mut self, _searcher: &grep_searcher::Searcher) -> Result<bool, Self::Error> {
        if self.context > 0 && self.truncated.is_none() {
            if self.out.len() + 3 > self.max_bytes {
                *self.truncated = Some(format!("结果达到 {} 字节上限", self.max_bytes));
            } else {
                self.out.push_str("--\n");
            }
        }
        Ok(self.truncated.is_none())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        _directory: tempfile::TempDir,
        input_path: PathBuf,
        roots: Roots,
        config: ToolsConfig,
    }

    fn fixture(format: RecordFormat) -> Fixture {
        let directory = tempfile::tempdir().unwrap();
        let input = directory.path().join("data");
        let output = directory.path().join("out");
        fs::create_dir_all(input.join("sub")).unwrap();
        fs::create_dir_all(output.join("workers").join("20260808T120000.000Z")).unwrap();
        fs::write(
            input.join("a.txt"),
            "苹果是水果。\n香蕉也是。\n橙子第三个。\n",
        )
        .unwrap();
        fs::write(input.join("sub").join("b.txt"), "这里有苹果树。\n").unwrap();
        fs::write(
            output.join(format!("1.{}", format.extension())),
            "完成记录提到苹果。\n",
        )
        .unwrap();
        fs::write(
            output
                .join("workers")
                .join("20260808T120000.000Z")
                .join("1.md"),
            "worker 档案提到苹果。\n",
        )
        .unwrap();
        let search = SearchToolConfig {
            enabled: true,
            max_result_bytes: 32 * 1024,
            max_in_flight: 4,
            max_matches: 100,
            max_context_lines: 20,
        };
        let read = ReadToolConfig {
            enabled: true,
            max_result_bytes: 32 * 1024,
            max_in_flight: 4,
        };
        Fixture {
            _directory: directory,
            input_path: input.clone(),
            roots: Roots {
                input: ReadRoot::open(input).unwrap(),
                output: Some(ReadRoot::open(output).unwrap()),
                output_format: format,
            },
            config: ToolsConfig {
                max_in_flight: 4,
                search,
                read,
            },
        }
    }

    #[test]
    fn frozen_input_rejects_same_size_content_replacement_with_restored_times() {
        let fixture = fixture(RecordFormat::Markdown);
        let source = fixture.input_path.join("a.txt");
        let metadata = fs::metadata(&source).unwrap();
        let modified = metadata.modified().unwrap();
        let accessed = metadata.accessed().unwrap();
        let original_len = metadata.len();
        let (frozen, _) = fixture.roots.input.freeze().unwrap();

        let replacement = "梨子是水果。\n葡萄也是。\n柚子第三个。\n";
        assert_eq!(replacement.len() as u64, original_len);
        fs::write(&source, replacement).unwrap();
        let file = fs::OpenOptions::new().write(true).open(&source).unwrap();
        file.set_times(
            fs::FileTimes::new()
                .set_modified(modified)
                .set_accessed(accessed),
        )
        .unwrap();
        fs::write(fixture.input_path.join("added.txt"), "later").unwrap();

        let error = frozen.open_file(Path::new("a.txt")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("快照内容"), "{error}");
        assert!(
            !walk_files(&frozen)
                .unwrap()
                .contains(&PathBuf::from("added.txt"))
        );
    }

    fn execute(fixture: &Fixture, name: &str, json: &str) -> ToolOutput {
        let value = serde_json::from_str(json).unwrap();
        registrations(
            &fixture.config,
            WorkerOutputAccess::Published,
            InputModalities::Text,
        )
        .into_iter()
        .find(|entry| entry.spec.name == name)
        .unwrap()
        .executor
        .execute(&fixture.roots, &value)
    }

    #[test]
    fn search_and_read_input() {
        let fixture = fixture(RecordFormat::Markdown);
        let found = execute(&fixture, "search", r#"{"pattern":"苹果","scope":"input"}"#);
        assert!(found.content.contains("== a.txt =="), "{}", found.content);
        assert!(found.cacheable);
        let read = execute(
            &fixture,
            "read",
            r#"{"scope":"input","path":"a.txt","start_line":2,"end_line":3}"#,
        );
        assert_eq!(read.content, "2: 香蕉也是。\n3: 橙子第三个。\n");
        assert!(read.cacheable);
    }

    #[test]
    fn output_sees_only_current_numbered_records() {
        let fixture = fixture(RecordFormat::Json);
        let found = execute(&fixture, "search", r#"{"pattern":"苹果","scope":"output"}"#);
        assert!(found.content.contains("1.json"));
        assert!(!found.content.contains("workers"));
        assert!(!found.cacheable);
        let denied = execute(
            &fixture,
            "read",
            r#"{"scope":"output","path":"workers/20260808T120000.000Z/1.md"}"#,
        );
        assert!(denied.content.starts_with("错误："));
    }

    #[test]
    fn isolated_workers_neither_advertise_nor_execute_output_access() {
        let mut fixture = fixture(RecordFormat::Markdown);
        fixture.roots.output = None;
        let registrations = registrations(
            &fixture.config,
            WorkerOutputAccess::None,
            InputModalities::Text,
        );
        for registration in &registrations {
            assert_eq!(
                registration.spec.parameters["properties"]["scope"]["enum"],
                serde_json::json!(["input"]),
                "{} 不得向模型暴露 output scope",
                registration.spec.name
            );
        }

        for (name, arguments) in [
            (
                "search",
                serde_json::json!({"pattern":"苹果","scope":"output"}),
            ),
            ("read", serde_json::json!({"scope":"output","path":"1.md"})),
        ] {
            let tool = registrations
                .iter()
                .find(|entry| entry.spec.name == name)
                .unwrap();
            let output = tool.executor.execute(&fixture.roots, &arguments);
            assert!(
                output.content.contains("无权读取已发布结果"),
                "{}",
                output.content
            );
        }
    }

    #[test]
    fn image_models_receive_a_path_only_read_image_tool() {
        let mut fixture = fixture(RecordFormat::Markdown);
        fixture.config.max_in_flight = 1;
        fixture.config.read.max_result_bytes = 1;
        let mut encoded = std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgb8(2, 3)
            .write_to(&mut encoded, image::ImageFormat::Png)
            .unwrap();
        fs::write(fixture.input_path.join("sample.png"), encoded.into_inner()).unwrap();
        fs::write(fixture.input_path.join("wrong.jpg"), b"not an image").unwrap();

        let registrations = registrations(
            &fixture.config,
            WorkerOutputAccess::None,
            InputModalities::TextAndImage,
        );
        let read_image = registrations
            .iter()
            .find(|entry| entry.spec.name == "read_image")
            .unwrap();
        assert_eq!(
            read_image.spec.parameters["required"],
            serde_json::json!(["path"])
        );
        assert!(
            read_image.spec.parameters["properties"]
                .as_object()
                .unwrap()
                .get("scope")
                .is_none()
        );

        let output = read_image
            .executor
            .execute(&fixture.roots, &serde_json::json!({"path":"sample.png"}));
        assert_eq!(output.images.len(), 1);
        assert!(output.images[0].data().byte_len() > 1);
        assert!(output.content.contains("2×3"));
        for arguments in [
            serde_json::json!({"path":"../sample.png"}),
            serde_json::json!({"path":"https://example.com/sample.png"}),
            serde_json::json!({"path":"sample.png","scope":"input"}),
            serde_json::json!({"path":"wrong.jpg"}),
        ] {
            let output = read_image.executor.execute(&fixture.roots, &arguments);
            assert!(output.content.starts_with("错误："), "{}", output.content);
            assert!(output.images.is_empty());
        }
    }

    #[test]
    fn read_rejects_escape_absolute_and_non_utf8() {
        let fixture = fixture(RecordFormat::Markdown);
        for path in ["../x", ".\\a.txt", "C:\\Windows\\win.ini"] {
            let output = execute(
                &fixture,
                "read",
                &serde_json::json!({"scope":"input","path":path}).to_string(),
            );
            assert!(
                output.content.starts_with("错误："),
                "{path}: {}",
                output.content
            );
        }
        fs::write(fixture.input_path.join("bad.txt"), [0xff, 0xfe]).unwrap();
        let output = execute(&fixture, "read", r#"{"scope":"input","path":"bad.txt"}"#);
        assert!(output.content.contains("UTF-8"), "{}", output.content);
    }

    #[cfg(unix)]
    #[test]
    fn read_rejects_symlink() {
        use std::os::unix::fs::symlink;
        let fixture = fixture(RecordFormat::Markdown);
        symlink(
            fixture.input_path.join("a.txt"),
            fixture.input_path.join("link.txt"),
        )
        .unwrap();
        let output = execute(&fixture, "read", r#"{"scope":"input","path":"link.txt"}"#);
        assert!(output.content.contains("符号链接"));

        symlink(
            fixture.input_path.join("a.txt"),
            fixture.input_path.join("link.png"),
        )
        .unwrap();
        let read_image = registrations(
            &fixture.config,
            WorkerOutputAccess::None,
            InputModalities::TextAndImage,
        )
        .into_iter()
        .find(|entry| entry.spec.name == "read_image")
        .unwrap();
        let output = read_image
            .executor
            .execute(&fixture.roots, &serde_json::json!({"path":"link.png"}));
        assert!(output.content.contains("符号链接"));
    }

    #[cfg(unix)]
    #[test]
    fn opened_root_is_not_rebound_when_ambient_path_is_replaced() {
        let directory = tempfile::tempdir().unwrap();
        let ambient = directory.path().join("data");
        let original = directory.path().join("original-data");
        fs::create_dir(&ambient).unwrap();
        fs::write(ambient.join("value.txt"), "原目录").unwrap();
        let root = ReadRoot::open(ambient.clone()).unwrap();

        fs::rename(&ambient, &original).unwrap();
        fs::create_dir(&ambient).unwrap();
        fs::write(ambient.join("value.txt"), "替换目录").unwrap();

        let mut value = String::new();
        stream_utf8_file(
            &root,
            Path::new("value.txt"),
            &CancellationToken::new(),
            |text| {
                value.push_str(text);
                true
            },
        )
        .unwrap();
        assert_eq!(value, "原目录");
        assert_eq!(walk_files(&root).unwrap(), [PathBuf::from("value.txt")]);
    }

    #[test]
    fn configured_limits_are_enforced_and_marked() {
        let mut fixture = fixture(RecordFormat::Markdown);
        fixture.config.search.max_matches = 1;
        let found = execute(&fixture, "search", r#"{"pattern":"苹果","scope":"input"}"#);
        assert!(found.content.contains("匹配数达到 1 上限"));
        assert!(!found.cacheable);
        fixture.config.read.max_result_bytes = 8;
        let read = execute(&fixture, "read", r#"{"scope":"input","path":"a.txt"}"#);
        assert!(read.content.contains("[cut]"));
        assert!(read.content.len() <= 8);
        assert!(!read.cacheable);
    }

    #[test]
    fn read_stops_at_end_line_and_bounds_a_giant_line() {
        let mut fixture = fixture(RecordFormat::Markdown);
        fs::write(fixture.input_path.join("early.txt"), b"first\n\xff").unwrap();
        let early = execute(
            &fixture,
            "read",
            r#"{"scope":"input","path":"early.txt","end_line":1}"#,
        );
        assert_eq!(early.content, "1: first\n");

        fixture.config.read.max_result_bytes = 32;
        fs::write(
            fixture.input_path.join("giant.txt"),
            format!("{}\n不会读取到这里", "苹".repeat(100_000)),
        )
        .unwrap();
        let giant = execute(
            &fixture,
            "read",
            r#"{"scope":"input","path":"giant.txt","end_line":1}"#,
        );
        assert!(giant.content.contains("已截断") || giant.content.contains("[cut]"));
        assert!(giant.content.len() <= 32, "{}", giant.content.len());
        assert!(!giant.cacheable);

        fs::write(fixture.input_path.join("carriage.txt"), b"tail\r").unwrap();
        let carriage = execute(
            &fixture,
            "read",
            r#"{"scope":"input","path":"carriage.txt"}"#,
        );
        assert_eq!(carriage.content, "1: tail\r\n");
    }

    #[test]
    fn search_rejects_a_line_over_its_heap_boundary() {
        let mut fixture = fixture(RecordFormat::Markdown);
        fixture.config.search.max_result_bytes = 1024;
        fs::write(
            fixture.input_path.join("one-line.txt"),
            "x".repeat(128 * 1024),
        )
        .unwrap();
        let found = execute(
            &fixture,
            "search",
            r#"{"pattern":"never-present","scope":"input","glob":"one-line.txt"}"#,
        );
        assert!(found.content.starts_with("错误：搜索"), "{}", found.content);
        assert!(found.content.contains("内存上限"), "{}", found.content);
        assert!(!found.cacheable);
    }

    #[test]
    fn cancelled_read_returns_an_uncacheable_error() {
        let fixture = fixture(RecordFormat::Markdown);
        let tool = registrations(
            &fixture.config,
            WorkerOutputAccess::Published,
            InputModalities::Text,
        )
        .into_iter()
        .find(|entry| entry.spec.name == "read")
        .unwrap()
        .executor;
        let cancel = CancellationToken::new();
        cancel.cancel();
        let output = tool
            .prepare(&serde_json::json!({"scope":"input","path":"a.txt"}))
            .unwrap()
            .execute_cancellable(&fixture.roots, &cancel);
        assert!(output.content.contains("取消"), "{}", output.content);
        assert!(!output.cacheable);
    }

    #[test]
    fn streamed_file_checks_cancellation_between_chunks() {
        let fixture = fixture(RecordFormat::Markdown);
        fs::write(fixture.input_path.join("large.txt"), "x".repeat(256 * 1024)).unwrap();
        let cancel = CancellationToken::new();
        let mut chunks = 0usize;
        let error = stream_utf8_file(
            &fixture.roots.input,
            Path::new("large.txt"),
            &cancel,
            |_| {
                chunks += 1;
                cancel.cancel();
                true
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        assert_eq!(chunks, 1, "取消后不得再解码下一块");
    }

    #[test]
    fn canonical_keys_merge_omitted_defaults() {
        let fixture = fixture(RecordFormat::Markdown);
        let tool = registrations(
            &fixture.config,
            WorkerOutputAccess::Published,
            InputModalities::Text,
        )
        .into_iter()
        .find(|entry| entry.spec.name == "search")
        .unwrap()
        .executor;
        let short = serde_json::json!({"pattern":"x","scope":"input"});
        let explicit =
            serde_json::json!({"literal":false,"scope":"input","context":0,"pattern":"x"});
        assert_eq!(
            tool.prepare(&short).unwrap().canonical_cache_key(),
            tool.prepare(&explicit).unwrap().canonical_cache_key()
        );
    }
}
