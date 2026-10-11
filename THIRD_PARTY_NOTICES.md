# Third-party notices and source publication acceptance

This candidate references public packages; it does not vendor their source,
wheels, market data or binary assets. The complete versioned inventory is
[dependency-notices.json](docs/dependency-notices.json). Exact upstream copyright,
license and NOTICE texts gathered from package archives are retained in
[third-party-license-texts.txt](docs/third-party-license-texts.txt), deduplicated by
SHA-256 and mapped to every package/archive path in the inventory.

Acceptance covers this source publication and the locked inputs. Preserve those
texts, installed package licenses and OS copyrights in images/distributions;
never strip upstream notices or relicense bundled components. The inventory
covers runtime, optional, development/build and architecture-specific packages.
Future lock/base-image/asset changes require fresh review. Final image SBOMs
identify actual redistributed OS/Python/build components; source-only notice
acceptance is not a new grant for additional binary redistribution.

## TradingView Lightweight Charts

TradingView Lightweight Charts™
Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/
Licensed under Apache-2.0. Its upstream license/NOTICE is included in the exact
text inventory. Keep the chart's built-in attribution logo/link in the UI.

## Optional SDK and repository-owned code

Shioaji's public metadata/archive provides no explicit license grant. Do not
infer permission to redistribute its binary SDK. The default Public runtime
images exclude it; optional installation/commercial use requires separately
accepted vendor terms. Adapter source/optional external dependency references
are reviewed independently. No private package notice or private install exists.
Platform/Core are repository-owned source; publication does not create a new
license grant or invent a missing LICENSE. Third-party terms cover their own
components only.

## Containers

Pinned Python/Debian, Node/Alpine build, Go/Alpine build, Alpine runtime and the
official Caddy buildable release artifact are listed by digest. The gateway
build fixes the Go toolchain and `golang.org/x/net` module versions and verifies
their build metadata. Retain their upstream licenses and OS package
copyright/license files; preserve Apache-2.0 Caddy and Python/Node/Go bundled
notices. Generated SBOMs are required for each final runtime/gateway image. No
image, font download or OS binary is included in the source manifest.

## Python locked identities

| Component | Version | License evidence |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| contourpy | 1.3.3 | See exact archive license texts and PyPI classifiers |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| cycler | 0.12.1 | See exact archive license texts and PyPI classifiers |
| fastapi | 0.141.1 | MIT |
| fonttools | 4.64.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| kiwisolver | 1.5.1 | See exact archive license texts and PyPI classifiers |
| matplotlib | 3.11.1 | See exact archive license texts and PyPI classifiers |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pandas | 3.0.6 | See exact archive license texts and PyPI classifiers |
| pillow | 12.3.0 | MIT-CMU |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pyjwt | 2.15.1 | MIT |
| pyparsing | 3.3.2 | MIT |
| python-dateutil | 2.9.0.post0 | Dual License |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| pyyaml | 6.0.3 | MIT |
| shioaji | 1.7.4 | NOASSERTION |
| six | 1.17.0 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| tw-quant-core | 1.2.0 | Repository-owned; no third-party license grant inferred |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| tzdata | 2026.3 | Apache-2.0 |
| uvicorn | 0.52.4 | BSD-3-Clause |
| uvloop | 0.22.1 | MIT License |
| watchfiles | 1.2.0 | MIT |
| websockets | 17.1 | BSD-3-Clause |

The npm inventory covers 406 exact locked paths; bundled attribution/license texts remain included even for build-only or optional platform packages.
