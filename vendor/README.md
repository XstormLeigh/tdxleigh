# Vendored wheels

`easy_tdx-1.32.7-py3-none-any.whl` is the same build as **easy-tdx 1.32.7** on Chinese PyPI mirrors (Tsinghua / Aliyun). It is checked in because **pypi.org does not list `easy-tdx`**, while `uv` defaults to pypi.org.

To refresh from your mirror:

```bash
pip download easy-tdx==1.32.7 --no-deps -d vendor/wheels
```

Then update the filename in `pyproject.toml` `[tool.uv.sources]` if the version changes.
