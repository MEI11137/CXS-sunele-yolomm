# C-02 文件存储与受保护访问接口

## 后台与旧调用兼容

`clients.file_client.FileClient` 保留 `upload / get / delete / download / get_file_url` 签名。后台检测、反馈、训练和视频代码调用本机存储，不再向无鉴权 HTTP 文件路由发请求。对象桶和键经过路径及链接解析检查，不能越出配置的 `FILE_STORE_PATH`；上传按块读取并遵守大小上限。业务调用方仍须先核实它操作的模型、数据集或记录权限，当前兼容适配本身没有可信业务身份。

## HTTP 入口

- `POST /api/file/upload/avatars`：需要 Bearer Token，限真实 JPG/PNG 和 2 MB，登记上传者。旧网页头像上传继续使用此入口。
- `POST /api/file/project/<project_id>/upload`：需要 Bearer Token 和服务端注入的 `PROJECT_FILE_ACCESS(user_id, project_id, action)`。回调必须明确返回 `True`，缺少回调或返回 `None` 均拒绝。支持 JPG、PNG、TIFF、ZIP、PT、PDF、DOCX 和 UTF-8 Markdown。旧 DOC 要先转 DOCX。上传记录所有者、项目、名称、类别、大小、SHA-256、版本 1 和 `uploaded` 状态；此状态只代表文件已保存，不代表数据集或权重已校验、登记或发布。
- `GET /api/file/metadata/<file_id>`：独立文件仅对所有者返回；项目文件按仍有效的 `file:read` 项目权限返回状态和引用字段。
- `GET /api/file/<bucket>/<object_key>`：Bearer Token 或登录时设置的文件预览 Cookie；优先查文件登记，独立文件按所有者、项目文件按 `file:read` 权限检查。旧业务记录仅在账号匹配且没有其他账号引用同一键时作为兼容依据。头像字段本身不提供读取授权。未登记、无项目权限、失效账号及不安全路径均拒绝。
- `DELETE /api/file/<bucket>/<object_key>`：仅允许上传者通过 Bearer Token 删除本人独立上传、尚未绑定项目的对象；后台业务文件仍由原业务流程处理。

登录 Cookie 为 HttpOnly，路径限 `/api/file`，只供图片和视频标签的 GET 请求使用；写请求仍要求 Authorization 头。前端普通请求启用 credentials，头像上传读取与其他业务请求相同的登录令牌位置。默认允许本机 `localhost:3000` 和 `127.0.0.1:3000` 跨域携带凭据；异机部署时设置 `FRONTEND_ORIGINS` 和浏览器可访问的 `FILE_PUBLIC_URL`（含 `/api/file`），并配置前端 API 地址。

JWT 签名密钥优先使用至少 32 字符的 `CXS_JWT_SECRET`；未设置时在运行文件存储目录创建 `.jwt-secret`，该文件已排除提交。升级后旧 Token 失效，用户需重新登录。保留此密钥才能使重启后已登录会话持续有效。

复核后补充了头像引用所有权校验、普通用户角色修改拒绝、检测保存和增强/分析入口的文件读取校验。删除旧检测记录仅清理经文件登记确认归属的对象。旧记录可能在修复前存有客户端伪造引用，历史数据仍须专项审计；未登记的旧头像需重新上传才能预览。

## 尚待联调

- 第二人的项目、资源归属和统一 `require_access` 尚未提供；`PROJECT_FILE_ACCESS` 未注入时项目文件接口正式运行会拒绝，不使用允许所有项目的替身。
- 旧数据集和模型表没有可靠的项目/所有者字段，直接文件 HTTP 链接不会开放这些未明归属资源。原业务下载路由及后台本机读写仍需接入统一资源权限。
- 项目文件的 `uploaded` 状态后续需由 C-03 的数据集/权重导入和 C-09 文档解析推进为相应业务状态。当前没有声称文件已解析或可训练。
- 旧网页图片、视频通过 Cookie 访问的浏览器端行为尚未在实际运行站点联调；当前验证覆盖服务端模拟请求和前端构建。

