# 发布包分片

完整的免安装包 `ScreenRecorder-win64-portable.zip`（38.3 MB / 40,140,660 字节）因为 GitHub 的单次上传体积限制，被切成 7 片放在本目录。

合并后即为完整包，解压得到 `ScreenRecorder` 文件夹，双击 `ScreenRecorder.exe` 就能用，不需要 Python，也不需要另装 FFmpeg。

## 合并

**Windows**：双击 `合并.bat`，它会自动拼接并输出 SHA256。

或手动：

```bat
cd release
copy /b part-01+part-02+part-03+part-04+part-05+part-06+part-07 ..\ScreenRecorder-win64-portable.zip
```

**Linux / macOS**：

```bash
cd release
cat part-* > ../ScreenRecorder-win64-portable.zip
```

## 校验

合并后的 SHA256 应为：

```
28e8639d7cb836e43418bc7437c7d2ec2f6308da7e05ed1cc18836451bac2bfa
```

Windows 校验：

```bat
certutil -hashfile ..\ScreenRecorder-win64-portable.zip SHA256
```

Linux / macOS：

```bash
sha256sum ../ScreenRecorder-win64-portable.zip
```

## 分片清单

| 文件 | 大小 |
| --- | --- |
| `part-01` ~ `part-06` | 各 6,291,456 字节 |
| `part-07` | 2,391,924 字节 |

合计 40,140,660 字节，与合并结果一致。

> 不想合并也可以自己构建：按 [../docs/BUILD.md](../docs/BUILD.md) 两条命令即可打出同样的包。
