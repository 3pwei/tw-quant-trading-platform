# GitHub Actions 執行量與成本盤點（2026-10-24）

## 範圍與結論

基準 master：`0c5f08055daa7a30b1dd9c275db78e1166f0ca0c`。已逐一閱讀 14 個 workflow，並核對最近 50 筆 runs、PR #71 及一筆完整 CI 的 job 時間。Repository API 顯示 visibility=public；所有工作均指定標準 `ubuntu-latest`，未發現 larger runner 或自架 runner。

公開 repository 的標準 GitHub-hosted Runner 運算分鐘免費。因此下面的減量是資源與等待時間改善，不能宣稱會降低實際美元帳單。帳戶其他 private repo、儲存、Packages、Codespaces 及歷史可見性不在本次已取得證據範圍。

PR #71 正在修補安全問題且修改 security.yml，本 PR 不重複或覆蓋它；既有 Debian 漏洞到期阻擋仍然保留。未合併、部署、派送 Candidate/P8/P9，亦未更新 Production 核准。

## 14 個 workflow

成本等級為依工作內容判斷的相對運算負擔，不是已收費金額。手動工作沒有自動背景頻率；timeout 是上限而非實際耗時。所有部署授權、環境與腳本門檻完整保留。

| 檔名（.github/workflows/） | 用途與主要成本 | 觸發條件 | 必要程度與處置 |
|---|---|---|---|
| ci.yml | 9 個展開 jobs：Python 3.11/3.12/3.14、Dashboard、P9、雙 image store、離線 Shioaji、public images；Docker/整合測試為高成本 | 原本所有 push（含分支/標籤）+ PR | 核心門檻；改為 master push + 所有 PR，取消同 PR 舊版 CI，完整保留 jobs |
| security.yml | 9 個展開 jobs：期限、Bandit、JS SAST、雙語 CodeQL、Gitleaks、Python/npm audit、3 image scans/SBOM；高成本 | master push + PR + 每週一 03:17 UTC（台灣 11:17）+ 手動 | 核心安全門檻；全部保留。兩語 CodeQL 是不同語言分析，並非重複 |
| security-exception-expiry.yml | Python 檢查期限、同步追蹤 issue、強制過期失敗；低成本 | 每日 02:23 UTC（台灣 10:23）+ 手動 | 必要的持續風險追蹤，保留每日頻率；cron 不保證準時執行 |
| staging-candidate.yml | 驗證 master CI/Security、取得受控 provider wheel、建立 A/B runtime 與 gateway、scan/push/manifest；高成本 | 僅手動，需 BUILD staging + 精確 SHA | 發版候選必要；保留一次建置、精確來源與 digest 證據，不和普通 CI 共用私有 image cache |
| deploy-staging.yml | 精確 manifest、SSH、部署 A/B/rollback、soak；高且隨 soak 增長 | 僅手動，candidate ID、確認字串、soak 30–360 分，預設60 | P8 驗收必要；runner 在 SSH 等待期間仍被占用，不能直接縮短30分鐘最低驗收；timeout360，極端 soak 加其他步驟可能超時，需另案設計 |
| staging-readonly-diagnostics.yml | 精確 master、attempt1、唯讀 SSH 與去敏證據；低成本，timeout10 | 僅手動，expected SHA + DIAGNOSE staging 確認 | 保留按需診斷；同 P8 staging 群組、不可取消進行中的部署 |
| deploy-lightsail.yml | Legacy 部署/rollback、CI/Security gate、遠端建置與公開端點驗證；中高成本，亦使用 Lightsail 資源 | 僅手動，master ancestor SHA + 明確 Production 確認 | 仍有 Legacy/rollback 用途；未證明無用前保留，不自動執行 |
| deploy-production.yml | P9 evidence verify + cutover 兩 jobs、P8 binding、不可變 image、失敗回復與90日證據；中高成本 | 僅手動，來源SHA/candidate/manifest/確認 | Production 核心保護，完整保留 |
| deploy-production-poc-clean.yml | clean PoC P9 驗證/部署兩 jobs、差異限制、P8與 master gates、回復；中高成本 | 僅手動，來源SHA/candidate/manifest/專用確認 | 特定部署路徑；保留與一般 cutover 的差異，不能因表面相似合併 |
| prepare-production-poc-clean.yml | 四份 Production 專用配置、SSH與去敏結果；低至中成本 | 僅手動，精確 master PREPARE 確認 | 有主機寫入；按需保留，不能因省成本改自動觸發 |
| production-approval-inventory.yml | 唯讀 SSH，僅去敏雜湊；低成本，timeout5 | 僅手動、master | 核准盤點必要，保留 |
| production-prerequisite-provision.yml | 隔離 Production prerequisites、受控設定寫入、90日證據；中成本，timeout30 | 僅手動、master、PROVISION 確認 | 保留環境/SSH/來源校驗 |
| production-readonly-preflight.yml | 唯讀主機狀態與核准/rollback hash 核對、90日證據；低成本，timeout15 | 僅手動、master | Production 預檢必要，保留 |
| production-rollback-refresh.yml | 封存 rollback 更新、保留歷史與90日證據；中成本，timeout30 | 僅手動、master、previous hash + REFRESH 確認 | 有主機寫入且屬回復保護，保留 |

## 可量化的重複執行

最近50筆 runs 中有9組 CI 同一來源 head SHA 同時出現 branch push 與 pull_request（其中含 Dependabot）。這是抽樣，不是整個月統計。PR 的 merge-context 與 branch head 測試環境可能不同；兩者不是同一個 merge tree。本次選擇保留更適合 PR 合併驗證的 PR CI，以及實際 master SHA 的 push CI。

具體證據：PR #71 的 `0f2d015c28b5ccdb2bde3e7b256fdd29a2411d58`：

- push CI #329： https://github.com/3pwei/tw-quant-trading-platform/actions/runs/40002029449
- PR CI #330： https://github.com/3pwei/tw-quant-trading-platform/actions/runs/40002039647
- PR Security #173： https://github.com/3pwei/tw-quant-trading-platform/actions/runs/40002039844

push CI #329 的實測（job started_at 到 completed_at，非帳單取整）：

| Job | 秒 |
|---|---:|
| public-images | 786 |
| P9 regressions | 6 |
| Python 3.11 / 3.12 / 3.14 | 119 / 111 / 121 |
| Dashboard | 38 |
| image-store false / true | 29 / 25 |
| offline Shioaji | 76 |
| 合計 | 1311（21.85 runner-minutes） |

這一筆 public-images 約占總 job 時間60%。每個原本同時觸發 push/PR 的更新，改後省一套 CI（9個 jobs，8次明確 Docker build 呼叫，其中2次為 scratch fixture）。PR 階段 CI run 數2→1，減少50%；若計入一個PR Security run，workflow數3→2，減少約33%，不代表總運算時間也降低33%。合併 master 的 CI/Security 仍照跑。以此單筆示例約省21.85 runner-minutes，不能外推成精確月節省額。

附帶影響：只有分支 push、尚未開 PR，以及 tag push 不再自動執行 CI；應以 draft PR 取得驗證。master push 與所有現有 PR 事件沒有路徑排除。CI concurrency 只取消同一PR過時的工作；master以run ID分組，不取消或排擠其他master SHA證據。Security沒有加入取消邏輯，定期風險與精確安全證據均保留。

## Docker、矩陣、路徑與快取評估

1. Docker：CI public-images 建2個 runtime target及兩種 gateway；offline Shioaji 建A/B fixture runtime；Security另建3個待掃描 image。它們驗證不同責任與來源，不應直接刪除。單一job內Docker已有layer reuse；跨job仍需重建。後續可量測各layer耗時，再對公開依賴層使用BuildKit cache；不能復用舊掃描結果，且仍須驗證當前來源與image bytes。Candidate帶私有wheel，禁止把它的層混入公開PR可讀cache。快取增加儲存、信任與失效管理，這次不新增。
2. Python矩陣：專案宣告 >=3.11,<3.15，3.11是最低、3.12常用、3.14上限版本；目前未涵蓋3.13也是現況而非本次刪除。3個Python jobs合計351秒，約27%，小於Docker主項。image-store false/true只有54秒，直接涵蓋Docker/OCI digest差異，不值得刪除測試分支。兩語CodeQL均保留。
3. 路徑過濾：暫不加workflow級paths-ignore。required workflow若被跳過會Pending，而job skip可能被視為成功；不能以skip冒充安全證據。此專案public-candidate.json覆蓋全部公開檔案，文件修改也改來源清單；目前部署依賴精確SHA的完整CI/Security。先建立可靠依賴圖與永遠執行的fail-closed聚合gate，再考慮Dashboard與純文件job級分流；unknown paths必須完整跑。不要對Gitleaks、漏洞期限、每週audit套文件跳過。
4. 既有npm cache：CI Dashboard、JS SAST、npm audit已以package-lock作cache key，仍用npm ci重建，不需要再重複加cache。Python可評估以OS/架構/Python/uv版本/lock雜湊隔離的下載cache；不快取整個venv、不省略uv lock --check或locked sync。
5. Artifact：SARIF30日、vulnerability/npm audit14日、P8 manifest/acceptance/diagnostics30日、P9關鍵證據90日。保留安全與發版證據，不批次縮短或刪除。SBOM action未明確設定retention，需核對實際artifacts大小/到期與repo預設。一般失敗診斷可另案依實際大小調整；不能刪除仍由Production核准引用的P8/Candidate證據。
6. soak：30–360分鐘SSH等待是明確資源佔用。未來可將host持久化驗收與runner輪詢拆開，但必須保存完整session/ordered ledger/checksum/故障補償/授權，不在本次小幅優化中改寫。

## CI方案比較

| 方案 | 金錢成本 | 維護與遷移負擔 | 判斷 |
|---|---|---|---|
| 保留GitHub Actions標準runner | 本公開repo標準運算分鐘免費；實際帳單仍需SKU/儲存查核 | 最低；原生PR checks、artifacts、environments、權限與精確run證據保持相容 | 建議，先消除重複CI |
| Actions自架runner | GitHub現行文件列runner使用免費；另付VM/電力/磁碟/網路/備份/維運時間 | 中高；需要臨時隔離runner、Docker清理、更新、非信任PR隔離；不得放在交易Production主機或讓公開PR碰到部署密鑰 | 目前免費hosted運算沒有明確金錢收益，不建議先搬 |
| GitLab CI | hosted依方案compute quota與超額計算；自架另付主機與維運 | 高；需mirror/webhook/身分與秘密管理；重建GitHub required checks、artifact來源校驗及P8/P9 run證據整合 | 可行但無已證明節省，不優先 |
| Jenkins自架 | 軟體本身不需商業授權費；付controller/agent主機、備份與管理 | 最高；controller/plugin安全更新、agent隔離、憑證、備份與GitHub狀態整合；目前部署腳本依賴GitHub Actions資料結構 | 對個人專案負擔過大 |
| 本機 pre-commit / scripts / act | 本機資源與人工；不能代表GitHub native CodeQL/部署驗證 | 適合作為push前快速lint/unit預檢，Docker與GitHub平台行為可能不同 | 補充使用，不能替代遠端安全與Production gates |

自架或更換CI須連帶修改verify-workflow-gates及P8/P9 evidence信任模型，不能僅把測試移走並留下綠色占位check。

## 保護設定與帳單仍待確認

- rulesets API回傳空清單；傳統master branch protection端點因GitHub App權限回403。這不等於沒有branch protection。Environment實際reviewer/保護與billing未取得；本次確認的是版本庫內的environment引用、手動授權、精確SHA/P8/manifest/rollback腳本契約，未修改設定。
- 需要帳單的產品、SKU、日期、repository歸屬、quantity、rate、gross amount、discount/net amount，才能分辨實際扣款與免費用量估值。
- 另核對帳戶其他private repo分鐘、Actions artifact/cache超額、Packages可見性/流量、Codespaces與歷史repo可見性。不要將AWS Lightsail常駐費用歸給Actions；改CI不會自動關掉VM。
- 最新50筆亦含Dependabot dynamic更新工作，並不是新增的workflow YAML；14個檔案數量不能當作所有帳戶用量總數。

## 驗證與後續

本次只調整ci.yml觸發/concurrency、加入此盤點並更新公開來源雜湊清單。未減少測試矩陣、安全掃描、部署環境、最低soak、證據期限、風險阻擋或私有邊界。PR仍需自動CI/Security的當前commit結果；master目前及PR #71的已知安全失敗不能因成本PR被豁免。詳細本地驗證與當前自動檢查狀態記於PR描述。

## 官方參考（2026-10-24查核）

- GitHub billing: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Trigger/paths/required checks: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- Concurrency: https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency
- GitLab compute: https://docs.gitlab.com/ci/pipelines/compute_minutes/
- Jenkins security: https://www.jenkins.io/doc/book/managing/security/
