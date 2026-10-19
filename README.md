# webhtv-plugins

WebHTV（默影视）**运行时模块仓库**。

这里存放的是可选的播放能力模块。宿主 App 默认只带 ExoPlayer 内核，其他能力（实时字幕语音识别、
MPV 播放核心、Python 爬虫运行时等）由用户按需安装。

**模块是独立签署的 APK**，由宿主通过系统 `PackageInstaller` 安装，宿主再从模块的
`nativeLibraryDir` 直接加载原生库。

---

## 为什么用 APK 而不是压缩包

| 维度 | 压缩包 | **APK（本仓库选型）** |
| --- | --- | --- |
| 完整性 | 自实现校验 | 清单摘要 + **平台校验 APK 签名** |
| 版本管理 | 自建注册表 | `PackageManager` 原生 |
| 安装路径 | 自建目录，需 `chmod` | 平台 `nativeLibraryDir`（可信、已实测） |
| 卸载 | 自实现清理 | 平台卸载，无残留 |
| 路径穿越 | 需自防 | 平台解压，无此面 |

---

## 仓库结构

```text
catalog.json                 宿主拉取的模块清单（唯一事实来源）
plugins/sherpa/              模块元数据（构建输入）
scripts/build_module.py      打包模块 APK
scripts/gen_catalog.py       由 dist/ 重新生成 catalog.json
dist/                        构建产物（不入库）
```

## 构建模块

需要 Python 3.10+、Android SDK（build-tools 的 `aapt2`/`zipalign`/`apksigner`，
以及 NDK 与 CMake —— 见下方「库的来源」），以及一份 WebHTV 源码用于取原生库。

```bash
# 构建全部模块的全部 ABI（CI 用的就是这一条）
python scripts/build_module.py --all

# 只构建一个模块
python scripts/build_module.py --module sherpa --abi arm64-v8a

# 指定现成的库文件
python scripts/build_module.py --module sherpa --abi arm64-v8a --native /path/to/libsherpa-onnx-jni.so

# 生成 catalog.json
python scripts/gen_catalog.py
```

### 库的来源

模块的库有两种来源，由 `MODULES` 里的 `source` 字段决定：

| `source` | 含义 | 例子 |
| --- | --- | --- |
| `jniLibs` | 取 WebHTV 的 `app/src/main/jniLibs/<abi>/` | sherpa、ijk |
| `assets` | 取 WebHTV 的 `app/src/<abi_flavor>/assets/mpv-libs/<abi>/` | mpv |
| `cmake` | **用 WebHTV 自己的 CMake 目标现场构建** | dv5 |
| `chaquopy` | 取 WebHTV 的 `chaquo/build/intermediates/`（Chaquopy 插件产出） | python |

`cmake` 这一种是必要的：`libexo_dovi_renderer.so` 是编译产物，WebHTV 仓库里只有
`app/src/main/cpp/exo_dovi_renderer.cpp` 与 `third_party/exo-dv5-native/prebuilt/*.a`，
没有已构建的 `.so` 可复制。现场构建也保证模块与宿主同源，不会因签入的二进制漂移。

`chaquopy` 同理，而且更重：Python 的 8 个库与 `assets/chaquopy/**` 全部是构建产物
（`git ls-files` 确认未跟踪），由 Chaquopy Gradle 插件产出，需要构建机上有
Python 3.10 与 pip 网络访问（`requirements.txt` 的 8 个依赖）。
因此 CI 在调用本脚本前必须先构建 WebHTV 检出。

模块可用 `payload` 字段声明「除原生库外还要带的目录树」，目前只有 `python` 用：
`assets/chaquopy/**` 是解释器启动所必需的，会在清单里逐个登记摘要。

构建时链接参数带 `-Wl,--build-id=none`。这不是随手加的：`catalog.json` 为每个产物固定
一个 SHA-256，宿主拒绝摘要不符的下载，而 build-id 由链接输出（含构建目录路径）导出，
开启时同一源码在不同目录构建会得到不同摘要（实测：两者均为 9,584,504 字节，
仅 `.note.gnu.build-id` 的 36 字节中有 20 字节不同）。关掉后同样两次构建逐字节一致。

`build_module.py` 会校验两件容易出错的事：

1. `lib/<abi>/*.so` 必须是 **Stored（不压缩）**。Android 只对能直接 mmap 的库暴露
   `nativeLibraryDir`；被 deflate 的条目会被解压到别处，加载路径会失效。
2. APK 必须已签名，且 manifest 需声明 `android:hasCode="false"` —— 模块不带 dex，
   否则平台会以 `INSTALL_FAILED_INVALID_APK: code is missing` 拒绝安装。

## 发布

```bash
python scripts/gen_catalog.py --tag modules
# 把 dist/*.apk 传到 Release tag "modules"，确保文件名与 catalog.json 中一致
```

`catalog.json` 里的 `url` 必须与产物实际可下载地址一致，`sha256` 必须与产物一致；
宿主要在交给安装器之前校验摘要，不一致会直接拒绝安装。

---

## 模块契约

### `catalog.json`

```json
{
  "schema": 1,
  "modules": [
    {
      "id": "sherpa",
      "packageName": "com.silent.runtime.sherpa",
      "compatibilityId": "sherpa-onnx-jni-v1",
      "versionCode": 1,
      "versionName": "1.0.0",
      "artifacts": [
        {
          "abi": "arm64-v8a",
          "minSdk": 24,
          "size": 23630316,
          "sha256": "…",
          "url": "https://github.com/Silent1566/webhtv-plugins/releases/download/modules/runtime-sherpa-1.0.0-arm64-v8a.apk"
        }
      ]
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `schema` | 清单版本；宿主不识别即整体拒绝 |
| `compatibilityId` | **宿主↔模块 ABI 契约**。宿主内置认可集合，不匹配即拒绝安装 |
| `versionCode` | 更新判断与防降级 |
| `sha256` / `size` | 完整性；宿主校验通过才会交给安装器 |

### 模块内 `assets/runtime.json`

```json
{
  "module": "sherpa",
  "packageName": "com.silent.runtime.sherpa",
  "versionCode": 1,
  "versionName": "1.0.0",
  "compatibilityId": "sherpa-onnx-jni-v1",
  "abi": "arm64-v8a",
  "library": "lib/arm64-v8a/libsherpa-onnx-jni.so",
  "hostLibraries": {},
  "files": [
    {
      "path": "lib/arm64-v8a/libsherpa-onnx-jni.so",
      "size": 23603240,
      "sha256": "…"
    }
  ]
}
```

宿主加载前会逐项校验：清单与 catalog 是否描述同一模块（id/包名/契约/版本/ABI）、
`hostLibraries` 的宿主副本摘要、`files[]` 每项的大小与摘要。任一不符即拒绝加载。

### `hostLibraries` —— 宿主提供的共享库

若模块依赖宿主已有的共享库（典型是 `libc++_shared.so`），必须在此声明**名称 → 期望摘要**，
并**不得**把该库打进自己的 APK。

原因是一个真实事故：两份 `libc++_shared.so` 会争抢同一个 soname，后加载的那份
`libmpv.so` 会因缺符号（`std::__ndk1::__from_chars_floating_point`）而 `dlopen` 失败，
表现为「几乎所有片源都报 MPV 加载媒体失败」。因此宿主只提供唯一一份，模块声明期望的摘要，
不符则明确报 `host library digest mismatch`。

`libsherpa-onnx-jni.so` 经 `readelf -d` 实测只依赖系统库（`libandroid`/`liblog`/`libm`/`libdl`/`libc`），
所以 sherpa 模块的 `hostLibraries` 为空。

---

## 当前模块

| id | 包名 | 契约 | arm64-v8a | 说明 |
| --- | --- | --- | --- | --- |
| `sherpa` | `com.silent.runtime.sherpa` | `sherpa-onnx-jni-v1` | 22.5 MB | 实时字幕语音识别（sherpa-onnx） |
| `mpv` | `com.silent.runtime.mpv` | `mpv-jni-ffmpeg-shared-v1` | 43.3 MB | MPV 播放核心（9 库） |
| `ijk` | `com.silent.runtime.ijk` | `ijk-jni-v1` | 9.9 MB | IJK 播放内核（3 库） |
| `dv5` | `com.silent.runtime.dv5` | `exo-dv5-renderer-v1` | 9.2 MB | 杜比视界 Profile 5 的 GPU 映射渲染器 |
| `python` | `com.silent.runtime.python` | `chaquopy-python310-v1` | 19.0 MB | Python 爬虫运行时（Chaquopy，8 库 + 载荷树） |

`sherpa`/`mpv`/`ijk` 的库取自 WebHTV 的签入路径，`dv5` 的库由 CMake 现场构建，
`python` 的库与载荷由 Chaquopy 插件产出（均见「库的来源」）。

`python` 是唯一不只带原生库的模块：Chaquopy 的解释器要从 `assets/chaquopy/**` 树
读 stdlib、requirements 与 bootstrap，所以那 20 个文件也随模块分发，
并在清单里逐个登记摘要（宿主在运行时读取前会全树校验）。

## 兼容性约束

- **契约先于内容**：`compatibilityId` 变化意味着宿主需要同步改动，必须是显式决定，
  不能靠内容兼容性猜测。
- **不降级**：宿主拒绝安装低于已装 `versionCode` 的模块。
- **模块不带 dex**：模型类必须已在宿主 APK 中；模块只提供原生库。
