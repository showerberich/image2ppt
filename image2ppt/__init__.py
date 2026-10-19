"""image2ppt — 调用 image2ppt.asia，把图片 / PDF 变成完全可编辑的 PPTX。

```python
from image2ppt import Client

with Client() as client:                     # 读环境变量 IMAGEPPT_TOKEN
    result = client.convert_and_wait("figure.png")
    print(result.path)
```
"""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WAIT_TIMEOUT,
    APIError,
    AuthenticationError,
    Client,
    Conversion,
    ConversionTimeout,
    ConvertResult,
    Image2PPTError,
    InsufficientCredits,
    Job,
    JobFailed,
    JobNotReady,
    NetworkError,
    PDFConfirmationRequired,
    default_token_file,
    mint_token,
    resolve_base_url,
    resolve_token,
)

__version__ = "1.0.0"

__all__ = [
    "APIError",
    "AuthenticationError",
    "Client",
    "Conversion",
    "ConversionTimeout",
    "ConvertResult",
    "DEFAULT_BASE_URL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WAIT_TIMEOUT",
    "Image2PPTError",
    "InsufficientCredits",
    "Job",
    "JobFailed",
    "JobNotReady",
    "NetworkError",
    "PDFConfirmationRequired",
    "__version__",
    "default_token_file",
    "mint_token",
    "resolve_base_url",
    "resolve_token",
]
