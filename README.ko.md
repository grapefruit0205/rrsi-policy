# rrsi-policy

[English](README.md) · **한국어**

**문서에 없는 팀 규칙을 Claude Code가 실제로 지키게 만드는 하네스로 바꾸고, 효과를 숫자로 확인하세요.**

Claude는 코드를 잘 짜지만 우리 팀 규칙은 모릅니다. 어떤 파일이 자동 생성이라 손대면 안 되는지, 변경
메모를 어디에 어떤 형식으로 남기는지, 새 기능을 플래그 뒤에 숨기는지, 배포를 어디까지 해도 되는지,
보고를 어떻게 쓰는지. 대부분 어디에도 적혀 있지 않아서 리뷰 때마다 같은 지적이 반복됩니다.

`rrsi-evolve`는 내 작업으로 만든 태스크를 Claude Code로 돌리고, 실패를 분석해 프로젝트의 하네스
(`CLAUDE.md`, 훅, 스킬)를 고칩니다. 그 수정은 **연습에 쓰지 않은 태스크(heldout)**에서 노이즈보다
확실히 오르고 늘어난 비용만큼 값어치가 있을 때만 남깁니다. Google Research
[RRSI](https://github.com/google-research/rrsi)의 하네스 개선 루프를 Claude Code용으로 옮긴 비공식
구현이며, Google 제품이 아닙니다.

**결과** (Opus 5.5, 문서에 없는 팀 규칙 8개를 둔 가상 저장소, heldout 태스크 7개 × 3회):

| | heldout 점수 | 팀 규칙을 모두 지킨 횟수 | 토큰/회 |
|---|---|---|---|
| 개선 전 | 0.724 | 0/21 | 78.9k |
| [repo 범위](#하네스-범위), 3라운드 | **1.000** | **21/21** | 74.8k (−5%) |
| general 범위, 5라운드 | 0.728 | 0/21 | 95.7k (+21%) |

긴 지시서 3개를 포함해 heldout 태스크가 모두 1.000이 됐습니다. 코드는 세 경우 모두 맞았고, 점수 차이는
전부 팀 규칙에서 났습니다. Opus는 일하는 법은 이미 압니다. 모르는 것은 그 팀만의 사실이고, 그걸 적을 수
있는 건 repo 범위뿐입니다. 채택된 하네스는 CLAUDE.md의 "Team conventions" 절, 생성 파일인 CHANGELOG
수정을 막는 PreToolUse 훅, 보고 제목이 빠지면 한 번 되돌려 보내는 Stop 훅, 관례 파일을 한 번에 훑는
조사 스크립트였습니다.

**CLAUDE.md를 직접 쓰면 되지 않나?** 쓸 수 있습니다. 루프는 손으로 하기 어려운 부분을 맡습니다.

- **무엇을 쓸지:** Claude가 실제로 틀린 지점에서 시작합니다. 보통 아무도 문서화할 생각을 못 한 것들입니다.
- **지키게 하기:** 자꾸 어기는 규칙은 긴 파일의 한 줄이 아니라, 행동을 막거나 되돌리는 훅이 됩니다.
- **효과 확인:** 모든 수정을 heldout으로 확인하고, 점수가 안 오르면 버립니다.
- **가볍게 유지:** 비용도 잽니다. 이득 없이 토큰만 늘리는 수정은 탈락합니다.

**어떻게 고르나:**

- **측정으로 고릅니다.** 변경 전후 점수를 비교하고, 노이즈 안에서 오른 점수는 받아들이지 않습니다.
- **꼼수를 막습니다.** trial은 샌드박스 안에서 돌아 채점기를 볼 수 없고, 더 센 모델로 바꾼 trial은
  0점입니다.
- **전부 남습니다.** 후보마다 git 커밋, diff, trial 기록이 남아 왜 채택되거나 떨어졌는지 볼 수
  있습니다.

**무엇을 테스트할지 모르겠다면** [`rrsi-evolve mine`](#대화-기록에서-태스크-찾기-mine)이 이
컴퓨터의 Claude Code 대화 기록을 읽어 내가 자주 고쳐 달라고 한 것("확인만 하랬잖아", "추정 말고
확인해", "한국어로 써")을 유형별로 보여 주고, 유형마다 태스크 아이디어를 붙여 줍니다. 모델을 부르지
않고, 기록은 이 컴퓨터 밖으로 나가지 않습니다.

**이런 분께 맞습니다:** Claude가 자꾸 어기는 관례가 있는 저장소의 팀(repo 범위), 또는 약한 모델을
서브에이전트로 쓰는 경우(general 범위). Linux 기준입니다.
**아직은 아닙니다:** 하네스를 가끔만 고치는 경우. macOS에서는 샌드박스 없이 돌아 점수를 속이기
쉽습니다. Windows는 그대로는 동작하지 않으니 WSL2를 쓰세요.

**모델:** 모든 모델 설정의 기본값은 `inherit`, 즉 지금 쓰시는 모델입니다. 측정과 판정은 그 측정을
돌린 모델에 대해서만 의미가 있어서, trial·탐색 역할·critic 모두 기본으로 그 모델로 돕니다
([어떤 모델을 쓰나](#어떤-모델을-쓰나)).

**비용:** haiku 기준으로 데모의 baseline(trial 8개)이 $0.17, 한 라운드가 $1.74였습니다. 쓰시는
모델(대부분 opus)로 돌리면 몇 배 듭니다([비용 경고](examples/evolve-demo/README.md#cost-warning)).
달러 금액은 Claude Code가 보고하는 API 환산 비용(`total_cost_usd`)입니다. `ANTHROPIC_API_KEY` 없이
Pro·Max 구독으로 로그인해 쓰면 호출마다 청구되지 않고 요금제의 사용량 한도에서 빠집니다. 그래서 긴
실행은 돈 대신 세션·주간 한도를 다 쓸 수 있습니다.

**상태: 실험 단계.** 지금까지 측정된 개선은 위의 repo 범위 결과(가상 저장소 한 곳이며 벤치마크가
아닙니다)와, 더 약한 모델인 GLM 5.3 flash 서브에이전트 프롬프트입니다. 후자는 general 범위에서 heldout
실패율이 8.8%에서 3.4%로(점수 0.912 → 0.966) 줄었습니다. Opus 5.5에서 general 하네스 개선은 효과가
없었습니다. 제 대화 기록으로 만든 태스크 묶음(어려운 태스크 18개)이 이미 거의 1.0이었습니다. 루프는
채점기가 볼 수 있는 규칙을 배웁니다. 리뷰 피드백을 강제되고 측정된 하네스로 바꿀 뿐, 아무도 검사하지
않는 규칙을 스스로 찾지는 못합니다. 먼저 `rrsi-evolve baseline`을 돌려 1.0보다 낮은지 확인하세요.
좋은 태스크와 채점기를 만드는 게 사실상 본 작업입니다.

---

[google-research/rrsi](https://github.com/google-research/rrsi)를 Claude Code 하네스(CLAUDE.md,
skills, agents, commands, hooks, settings, MCP 설정, memory)에 가져온 도구 두 개입니다.

- **`rrsi-policy`**: 런타임 정책 엔진입니다. 하네스 파일이 바뀌기 **직전에** 별도의 `claude -p`
  critic이 과적합, 무의미한 편집, 무한 루프, 정책 우회를 걸러냅니다. 판정과 측정 결과는 원장에
  쌓여 다음 판정에 다시 쓰입니다.
- **`rrsi-evolve`**: RRSI의 탐색 루프 전체(Algorithm 1, 2)를 옮긴 것입니다. 사용자의 태스크
  묶음을 기준으로 Claude Code 프로젝트 설정을 진화시킵니다. 고정 정책과 탐색 역할 모두 헤드리스
  `claude -p`로 돌립니다. [rrsi-evolve](#rrsi-evolve-rrsi-루프-전체) 참고.

RRSI와 같은 부분과 다른 부분은 [RRSI와의 관계](#rrsi와의-관계)에 정리했습니다.

의존성: Python 3.10+ 표준 라이브러리, `claude` CLI.

## rrsi-policy의 장점

- **강제됩니다.** skill이나 CLAUDE.md에 적은 규칙은 모델이 무시할 수 있습니다. PreToolUse hook은
  편집이 디스크에 닿기 전에 실행되므로 건너뛸 수 없습니다.
- **critic이 독립적입니다.** 편집한 에이전트와 별개의 프로세스, 새 컨텍스트에서 돕니다. 모델은
  기본으로 세션이 쓰는 모델을 세션 기록에서 읽어 씁니다(정책 설정의 `"model"`로 고정 가능). 그래서 편집한 쪽의 자기 합리화를 보지 않고 diff만 보고 판단합니다. 출력은
  `--json-schema`로 형식이 강제됩니다.
- **정책 선택은 LLM이 하지 않습니다.** 정책은 argv, 도구 이름, 경로, 명령 정규식으로 결정론적으로
  고릅니다. diff 안의 텍스트가 느슨한 정책을 고르게 만들 수 없습니다.
- **비용이 낮습니다.** 하네스 파일 편집에만 LLM을 붙입니다. 나머지 쓰기는 secret 정규식만 검사합니다.
  MCP를 격리하면 haiku 기준 건당 약 $0.01입니다(실측, 격리 전 $0.18).
- **"좋아 보인다"가 아니라 측정값으로 판단합니다.** eval 명령으로 ΔS를 재고, 노이즈 대역 안의 향상은
  받아들이지 않습니다. 토큰 비용 증가는 점수 향상으로 정당화돼야 합니다. 거부되면 스냅샷으로
  되돌립니다.
- **실패를 기억합니다.** critic의 거부와 측정상 거부된 편집의 의도가 다음 critic 입력에 들어갑니다.
  그래서 같은 시도가 다시 걸립니다.
- **기본값이 안전합니다.**
  - `allow`를 절대 내보내지 않습니다(사용자 권한 프롬프트를 우회하지 않음).
  - 오류가 나거나 판단이 애매하면 `ask`로 넘깁니다. 같은 파일이 연속으로 거부되면 사용자에게 넘깁니다.
  - 엔진 자신의 설정과 원장을 고치려는 편집은 항상 `ask`입니다.
  - 재귀 가드가 있고, eval 실행 중에는 엔진이 꺼집니다.
- **hook 밖에서도 쓸 수 있습니다.** `check`가 exit code(0/2/3)를 돌려주므로 git hook이나 CI에도
  붙일 수 있습니다.

## 구조

```
PreToolUse ─▶ route (결정론적: argv --policy > 도구·경로 glob·명령 정규식)
               │
               ├─ fixed   항상 ask (엔진 자체 설정·원장 보호)
               ├─ regex   secret 패턴 → deny (LLM 없음, 추가된 줄만 검사)
               └─ llm     precheck → 편집 예산 → claude -p critic (--json-schema)
                            accept → 출력 없음 (사용자 권한 흐름 그대로)
                            reject → deny + 사유 (에이전트가 고쳐서 재시도)
                            uncertain / 오류 / 연속 거부 max_repairs → ask
PostToolUse ─▶ 실제 반영된 편집만 landed 기록 (+ 편집 전 스냅샷)
```

자식 `claude -p`는 다음 조건으로 실행됩니다.

| 설정 | 이유 |
|---|---|
| `RRSI_POLICY_ACTIVE=1` | 재귀 방지 |
| `--tools ""` | 도구 사용 금지 |
| 중립 cwd | 검토 대상인 CLAUDE.md를 읽지 않게 |
| `--strict-mcp-config`, `--setting-sources project` | MCP와 사용자 플러그인 격리 |

## 설치

Claude Code 안에서:

```
/plugin marketplace add grapefruit0205/rrsi-policy
/plugin install rrsi-policy@rrsi-policy
```

또는 내려받아 한 세션에만 불러올 수 있습니다.

```bash
git clone https://github.com/grapefruit0205/rrsi-policy
claude --plugin-dir ./rrsi-policy
```

`rrsi-evolve`는 일반 명령입니다. 내려받은 폴더의 `rrsi-policy/bin/rrsi-evolve`를 실행하거나
`bin/`을 PATH에 넣으세요. 설정을 바꾸려면
`policies/policies.json`을 `~/.config/rrsi-policy/policies.json`으로 복사해 수정합니다.
프로젝트 안의 설정 파일은 에이전트가 고칠 수 있으므로 읽지 않습니다.

## 측정 루프 (ΔS)

critic은 변경이 좋아 보이는지만 판단할 수 있습니다. 실제로 도움이 됐는지는 eval 명령으로 잽니다.
eval 명령은 마지막 stdout 줄에 숫자 하나, 또는 `{"S": 0.82, "C": 15300}`을 출력하면 됩니다.
C는 실행당 토큰 수이고 생략할 수 있습니다.

```bash
rrsi-policy baseline --cmd "./evals/run.sh" --k 3     # 현재 상태 점수 + 노이즈 대역 δ = 2·sd·√(2/k)
# ... Claude Code로 하네스 편집 (critic 통과분이 대기 묶음으로 쌓임)
rrsi-policy status                                    # incumbent, S*, δ, 대기 편집, yield, prune set, stall
rrsi-policy measure --cmd "./evals/run.sh" --k 3      # 묶음 판정: ACCEPTED(0) / REJECTED(1)
rrsi-policy revert                                    # 거부된 묶음의 파일을 스냅샷으로 복원
```

판정 규칙(RRSI Algorithm 2)은 다음과 같습니다.
- S'가 S* − δ보다 낮으면 거부합니다(S*는 지금까지의 최고 점수).
- ΔS가 δ보다 크면, 토큰 증가율이 β0 + β1·ΔS 이하일 때만 채택합니다.
- ΔS가 δ 이내면, 토큰을 절감했거나 처음 들어오는 구조적 컴포넌트(skill, agent, hook, mcp, memory)일
  때만 채택합니다.

측정 결과는 다음 런타임 판정으로 돌아옵니다.
- 측정상 거부된 편집의 의도가 critic 입력의 RECENT REJECTIONS에 들어갑니다.
- 최근 이득이 없는 컴포넌트(prune set), 정체 여부(stall), 아직 안 건드린 컴포넌트가 critic에 전달됩니다.
- baseline이 있는 프로젝트에서 측정 안 된 편집이 `max_pending`(기본 3)개 쌓이면 `ask`로 멈춥니다.
- eval 실행 중에는 `RRSI_POLICY_ACTIVE=1`이 설정되어, eval 안의 Claude 세션에서는 엔진이 꺼집니다.

baseline이 없으면 측정 기능은 꺼져 있고 critic만 동작합니다.

## rrsi-evolve: RRSI 루프 전체

`rrsi-policy`는 다른 누군가가 제안한 편집을 감독합니다. `bin/rrsi-evolve`는 탐색 자체를 돌립니다.
한 라운드는 다음과 같습니다.

1. incumbent(`evolve/claudecode` 브랜치 끝)를 태스크 묶음에서 태스크당 k번 평가합니다. trial
   하나는 새 임시 작업 공간에서 하네스를 프로젝트 설정으로 깔고 돌리는 `claude -p` 세션 하나이고,
   끝나면 숨겨진 `check.sh`가 채점합니다.
2. 다이제스터와 analyst가 실패·성공 trace를 읽고 세 가지 관점의 보고서를 씁니다.
3. 제안자 m명이 각자 git worktree에서 후보를 만듭니다. 편집 예산 b_t는 코사인으로 줄어듭니다.
   프롬프트에는 이력 원장, prune set, 정체(stall) 여부, 예약된 탐색 슬롯이 들어가고, 제안자는
   `done()`으로 선언 편집, 컴포넌트 태그, 예측, 사후 검증을 반드시 내야 합니다.
4. critic이 초안을 걸러냅니다(결정론적 precheck 후 LLM 검토, 제한된 수리 반복). smoke 실행으로
   하네스가 실제로 로드되는지도 확인합니다.
5. 살아남은 후보를 평가하고, Algorithm 2로 채택 가능한 후보 중 최고를 골라 브랜치를
   fast-forward합니다. 결과는 모두 다음 라운드를 위해 기록됩니다.

```bash
cd your-repo                                   # git 저장소
/path/to/rrsi-policy/bin/rrsi-evolve init      # rrsi.json, harness/CLAUDE.md, 예제 태스크 2개
git add -A && git commit -m "rrsi: initial harness"
rrsi-evolve smoke                              # 하네스가 로드되고 도는지
rrsi-evolve baseline                           # H_0 평가, frontier 시작
rrsi-evolve round --t 0 --dry-run              # 분석만
rrsi-evolve run                                # T까지 라운드 반복 (STOP 파일로 중단)
rrsi-evolve heldout --label champ              # heldout 분할에서 incumbent 평가
rrsi-evolve mine                               # 대화 기록에서 태스크 아이디어 찾기 (로컬 전용)
```

[examples/evolve-demo](examples/evolve-demo/)에 태스크 5개짜리 예제가 있습니다. 태스크 형식,
숨겨진 checker 작성 규칙, 비용 내역이 들어 있습니다. `readjudicate`, `reevaluate`, `calibrate`,
`status`는 RRSI와 똑같이 동작합니다.

태스크를 대화로 만들 수도 있습니다. `"turns"`에 사용자 메시지 여러 개를 넣으면 한 세션 안에서 앞
답이 끝날 때마다 하나씩 보내고, checker는 trial의 stream-json 대화 기록을 `RRSI_STREAM`으로 받아
파일 변경뿐 아니라 에이전트가 한 답도 채점할 수 있습니다.

**RRSI 코드 그대로인 부분.** selection, history, components, evaluate, calibrate, critic,
digester, analyst 모듈은 주석을 빼면 RRSI 코드 그대로입니다. schedule, git 처리, proposer, loop,
driver, CLI는 목록으로 정리된 작은 변경만 있습니다(경로를 사용자 저장소 기준으로, LLM 호출 방식
교체, b_t 스케줄의 끝점).

**Claude Code용으로 새로 만든 부분.**
- LLM 호출은 Vertex 대신 도구 없는 `claude -p`(또는 Anthropic API)로 역할을 돌립니다.
- `claudecode` 도메인이 trial마다 작업 공간에 하네스를 깔고 정책을 돌린 뒤, stream-json trace를
  읽고 태스크의 checker로 채점합니다. C는 subagent를 포함한 세션 전체 토큰입니다.
- 제안자 헌법(SKILL.md, PATTERNS.md)은 RRSI의 구조와 규칙을 유지하면서 Claude Code 동작에 맞게
  다시 썼습니다(hooks, skills, subagents, settings, MCP, 그리고 헤드리스 trial에서 적용되는 것과
  안 되는 것).
- 정책이 실제로 고정됩니다. 자식 프로세스 환경을 정리해서 부모 Claude Code 세션의 설정이 새어
  들어가지 않습니다. 도구 집합을 고정하고 subagent를 정책 모델로 묶습니다. 이 고정값은
  `--settings`로 넣는데, 하네스 자신의 settings 파일보다 우선합니다(2.1.280에서 실측). 하네스가
  settings 키, env 변수, frontmatter로 모델·effort·thinking·advisor·fallback 모델·플러그인·자동
  메모리·공급자·권한을 바꾸면 precheck와 smoke 게이트에서 거부합니다. 하네스 스크립트가 `claude`,
  모델 API·SDK를 부르거나 `$HOME` 아래에 쓰는 것도 거부합니다. trial 안에서 `claude`를 다시
  부르면 실패합니다. `policy_effort`를 지정하면 정책의 추론 effort도 고정됩니다.

**한계.**
- RRSI는 trial을 컨테이너에서 돌렸습니다. 여기서는 기본값 `sandbox: "auto"`일 때(`RRSI_EVOLVE_SANDBOX`로도
  지정 가능) bubblewrap이 있는 Linux에서 trial마다 별도의 bwrap PID·IPC·마운트·네트워크 네임스페이스에서
  돌립니다. 저장소, runs 디렉터리, 공유 임시 디렉터리, 다른 trial이 보이지 않고, 에이전트가 띄운
  프로세스는 trial과 함께 모두 종료됩니다. 나머지 호스트는 읽기 전용입니다. `/run`과 사용자 런타임
  디렉터리는 비어 있고 호스트의 다른 유닉스 소켓도 모두 가려서, 호스트 데몬(docker, 시스템·세션 버스,
  systemd)에 닿을 수 없습니다. `$HOME`은 trial마다 새로 만드는 빈 쓰기 가능 tmpfs입니다. 읽기 전용으로
  다시 연결하는 것은 `sandbox_home`의 도구 디렉터리(`~/.local/bin`, `~/.nvm`, `~/.cargo/bin`, mise, asdf,
  pnpm, conda, `~/.gitconfig` 등. `sandbox_home`은 이 목록에 추가하고, `sandbox_home_only: true`면
  대체합니다), `$HOME` 아래의 `PATH` 디렉터리 전부, 정책 자신의 인터프리터, `NODE_EXTRA_CA_CERTS`·
  `SSL_CERT_FILE` 등이 가리키는 CA 번들, 그리고 Bedrock·Vertex·Foundry를 쓸 때 그 인증 정보(`~/.aws`,
  `~/.config/gcloud`, `~/.azure`. SSO 로그인은 실행 전에 갱신하세요)입니다. 사용자 수준 설치(`pip --user`,
  `npm -g`)는 읽기 전용이라 워크스페이스 안에 설치해야 합니다. 호스트 경로를 쓰기 가능하게 연결하려면
  `sandbox_rw`에 적습니다(`/`, `$HOME`과 그 상위, 임시·런타임 디렉터리, Claude Code 자신의 데이터는
  안 됩니다). 첫 trial 전에 실제 샌드박스 구성에서 정책을 한 번 실행(`--version`)해 보고, 실행되지
  않으면 이유를 출력하고 멈춥니다. 커밋 뒤에 디스크에서 하네스가 바뀐 후보는 평가에서 실패 처리합니다. 네트워크 네임스페이스에는 바깥으로 가는 경로가 없고, 나가는 길은 허용 목록
  HTTPS 프록시 하나뿐입니다. 이 프록시는 Claude Code의 API·로그인 호스트의 443 포트와 설정된 모델
  게이트웨이(`ANTHROPIC_BASE_URL`, Bedrock·Vertex·Foundry 호스트. localhost 게이트웨이는 포트를 그대로
  전달합니다)로만 CONNECT 터널을 엽니다. 호스트가 HTTPS 프록시를 쓰면 터널도 그 프록시를 거칩니다. 평문
  HTTP는 프록시하지 않고, git은 허용한 HTTPS 원격만 됩니다(trial에는 SSH 키나 서명 키가 없습니다).
  PyPI, npm, git 호스트가 필요한 태스크는 `sandbox_net_allow`에 호스트를 추가합니다(다른 포트는
  `sandbox_net_ports`). `sandbox_network: "host"`로 두면 호스트 네트워크를 그대로 쓰는데, 이때는 호스트의
  abstract 소켓(X11)도 공유됩니다. 거부한 호스트와 이유는 실행 로그에 나오고, trial 안에서 시작하지
  못하는 하네스 MCP 서버는 smoke 게이트에서 실패합니다. Claude Code는 trial마다 새 설정 디렉터리를
  씁니다. `~/.claude`(모든 세션의 transcript, 사용자 자신의 skill·agent·settings·CLAUDE.md)와
  `~/.claude.json`은 보이지 않고 인증 파일만 연결됩니다. 이 파일은 쓰기 가능하게 연결되므로 토큰 갱신이
  원래 파일에 반영되지만, trial이 덮어쓸 수도 있습니다. Console API 키 로그인은 키를 그대로 씁니다.
  설치된 `claude` 바이너리 자리에는 실행을 거부하는 스텁이 덮이고, 세션이 정책 모델(또는 Claude Code의
  보조 haiku)이 아닌 모델을 쓴 trial은 0점입니다. 그래도 작정한 에이전트는 모델에 닿을 수 있습니다.
  정책이 돌려면 정책 바이너리와 인증 정보를 쓸 수 있어야 하고 API 호스트도 허용되기 때문입니다. 그런
  시도를 담은 하네스는 precheck와 critic이 거부합니다. 호스트 소켓은 trial이 시작할 때 있는 것만
  가립니다. 그 뒤에 데몬이 `/run`, `/tmp`, `$HOME` 밖에 만든 소켓이나 다른 네트워크 네임스페이스(컨테이너
  런타임)에서 만든 소켓은 가리지 못합니다.
- bubblewrap이 없으면(macOS, 비특권 user namespace가 없는 커널, 또는 `sandbox: "none"`) 정책은
  사용자 권한, 호스트 네트워크, 실제 `~/.claude`로 호스트에서 돕니다. 저장소 위치를 알려 주지 않고,
  워크스페이스 위쪽의 지시 파일을 제외하고, trial별 환경 토큰으로 남은 프로세스를 종료합니다.
  그래도 찾으려고 하면 checker를 찾을 수 있어서(프로세스 조상을 따라가면 저장소가 나오고, 환경을
  비운 프로세스는 토큰 검사를 피합니다) 실행 시 경고를 출력합니다. 다른 샌드박스(firejail,
  컨테이너)는 `policy_wrapper`로 지정하고, `sandbox: "bwrap"`으로 두면 샌드박스가 필수가 됩니다.
- <a id="하네스-범위"></a>**하네스 범위.** general 범위(`harness_scope: "general"`)에서는 제안자와 critic이
  과제 저장소에만 해당하는 내용을 모두 과적합으로 봅니다. 처음 보는 저장소에서도 도움이 되어야 하기
  때문입니다. 그래서 팀만의 규칙(아무도 손대지 않는 생성 파일, 정해진 보고 제목 같은 것)도 배우지
  못합니다. 이런 규칙은 일반 절차로는 알아낼 수 없습니다. `"harness_scope": "repo"`는 저장소 하나에만
  설치할 하네스용입니다. 과제(heldout 포함)도 그 저장소에서 나와야 합니다. 이 모드에서는 저장소 전체의 관례를
  적을 수 있고(보통 CLAUDE.md의 "Team conventions" 절과 그 규칙을 지키게 하는 훅), 과제 id·티켓 번호·과제별
  값은 여전히 거부됩니다. `rrsi-evolve init`은 `"repo"`를 적고, 이 키가 없는 설정은 예전처럼 `"general"`로
  동작합니다. 여러 프로젝트에서 같이 쓸 하네스(`~/.claude`, 서브에이전트 프롬프트)는 `"general"`로 두세요.
  `round`·`run`은 적용 중인 범위를 출력합니다. 팀 규칙이 문서로 남지 않은 가짜 저장소에서 Opus 5.5의 heldout
  점수가 repo 모드에서는 비용 변화 없이 0.724→1.000, general 모드에서는 비용 48% 증가에 0.728이었습니다.
- <a id="어떤-모델을-쓰나"></a>**어떤 모델을 쓰나.** rrsi-evolve의 `policy_model`, `proposer_model`,
  `analyst_model`, `critic_model`과 rrsi-policy critic의 `"model"`은 기본값이 `"inherit"`입니다.
  세션 자신의 모델(critic만), 없으면 `RRSI_MODEL`, `ANTHROPIC_MODEL`, 프로젝트의
  `.claude/settings.local.json`·`.claude/settings.json`, `~/.claude/settings.json`
  (`$CLAUDE_CONFIG_DIR`) 순서로 찾고, 아무것도 없으면(또는 볼 수 없는 계정 기본값인 `"default"`면)
  `opus`입니다. 평가하는 명령은 정책 모델과 그 출처를, `round`·`run`은 탐색 역할의 모델도 출력하고,
  critic은 판정마다 모델과 출처를 ledger에 남깁니다. 실행 디렉터리는 처음 평가한 정책 모델을 기억하고
  (`.rrsi/runs/<domain>/policy_model.json`), 나중에 다른 모델로 잡히면 멈춥니다(`opus[1m]`과
  `claude-opus-5-5`는 같은 모델로 봅니다). 두 모델의 점수는
  비교할 수 없기 때문입니다. 계속하려면 `"policy_model"`을 기록된 값으로 고정하고, 새 모델로 다시
  시작하려면 `--runs`로 새 디렉터리를 쓰세요. 일부러 싼 모델을 쓰려면 이름(`haiku`, `sonnet`,
  `opus`, 전체 id)을 적으면 됩니다.
- 한 job의 trial은 병렬로 돌고(`concurrency`), 여러 job도 동시에 돌 수 있습니다(`eval_parallel`).
  실행마다 별도의 전용 디렉터리, 프록시, 설정 디렉터리를 쓰고, job별 상태는 trial이 끝날 때마다
  저장합니다.
- 실제 비용이 듭니다. 평가 한 번이 |태스크| × k 세션이고, 역할의 턴 하나하나가 `claude -p`
  호출입니다. 데모 실측으로 baseline(haiku 세션 8개)은 $0.16, 역할을 모두 haiku로 둔 라운드 하나는
  역할 호출 116번에 $1.74였습니다. `smoke`, `baseline`, `--dry-run`부터 시작하세요.
- 정책이 이미 다 푸는 태스크 묶음에서는 얻을 것이 없습니다. 데모는 haiku로 baseline부터 S = 1.0이라
  라운드가 받아들일 수 있는 것은 토큰 절감이나 새 구조적 컴포넌트뿐입니다. 하네스가 실패하는
  태스크를 넣어야 합니다.
- k = 2에서 trial 결과가 서로 같으면 bootstrap으로 추정한 δ는 0입니다. `delta`를 직접 정하거나
  (데모는 8개 중 trial 하나) 기준 평가를 여러 번 해서 calibrate하세요.

## 대화 기록에서 태스크 찾기 (mine)

태스크 묶음에는 지금 하네스가 실패하는 태스크가 필요합니다. 무엇이 실패하는지 모르겠다면 대화
기록에 물어보세요.

```bash
rrsi-evolve mine                         # Markdown 보고서를 화면에 출력
rrsi-evolve mine --lang ko --out report.md
rrsi-evolve mine --project myapp --days 30 --json
```

`$CLAUDE_CONFIG_DIR/projects`(기본 `~/.claude/projects`)를 읽어, 직전 답변을 사용자가 고쳐 달라고
한 메시지를 찾고 실패 유형별로 나눕니다. 유형은 시킨 것보다 더 함, 확인하지 않고 추정함, 다른
언어로 답함, 설명 형식이 맞지 않음, 요청을 잘못 이해함, 같은 실패를 반복함, 결과가 동작하지 않음입니다.
유형마다 건수, 발췌 몇 개, 태스크 아이디어(작업 폴더에 무엇을 넣고 `check.sh`가 무엇을 볼지)를
보여 줍니다. 중단 횟수와, 질문과 다른 문자로 쓴 답변 수도 셉니다.

- **로컬에서만 돕니다.** 모델을 부르지 않고 어디로도 보내지 않습니다. 저장소나 `rrsi.json`도
  필요 없습니다.
- **가렸지만 여전히 개인 기록입니다.** 흔한 키·토큰 형식, URL과 `key=value`에 든 비밀번호, 이메일,
  홈 경로는 가리고 발췌는 짧게 자릅니다. 특이한 형태로 적힌 비밀값은 빠져나갈 수 있고, 내 대화라는
  점도 같습니다. `--out` 파일은 권한 0600으로 쓰고 심볼릭 링크에는 쓰지 않습니다. 공유하기 전에
  읽어 보세요.
- **대략적인 값입니다.** 영어·한국어 키워드로 찾습니다. 이어 하기로 겹친 메시지는 한 번만 세고,
  헤드리스 `claude -p` 세션(critic, trial, 스크립트)은 `--include-headless` 없이는 건너뜁니다.
  후보를 고르는 데 쓰고, 태스크는 직접 만드세요([examples/evolve-demo](examples/evolve-demo/) 참고).
  1~2개는 `"split": "heldout"`으로 빼 두세요.

## RRSI와의 관계

RRSI는 하네스를 **자동으로 탐색하는 최적화 루프**입니다. 제안자가 후보를 만들고, 병렬로 평가해서
가장 좋은 것을 고릅니다. `rrsi-evolve`가 그 루프입니다. `rrsi-policy`는 **사람이나 에이전트가 하는
편집을 감독하는 관문**이라서, 선택 규칙의 수식은 그대로 가져왔지만 탐색 루프에 해당하는 부분은
없거나 약하게만 들어 있습니다. 아래 표는 관문을 RRSI와 비교한 것이고, 마지막 열은 `rrsi-evolve`의
상태입니다.

| RRSI 기능 | rrsi-policy | 상태 | rrsi-evolve |
|---|---|---|---|
| 노이즈 하한 S' ≥ S* − δ | `selection.judge` | 수식 동일 | RRSI 코드 |
| 비용 규칙 ΔC ≤ β0 + β1·ΔS, 대역 내 shaped 점수 | `selection.cost_rule` | 수식 동일. 기본값은 다름(β1 = 10, 점수 척도 무관하게 설정) | RRSI 코드와 기본값 |
| novelty ν (구조적 컴포넌트) | `selection.novelty` | 동일. K_str = skill, agent, hook, mcp, memory | RRSI 코드, Claude Code 컴포넌트 태그 |
| 편집 단위 기록, 묶음 전체가 같은 ΔS를 받음 | `Ledger.measured_edits` | 동일 | RRSI 코드 |
| tried T, yield g, prune set B, stall σ, untried U | `Ledger.summary` | 계산은 동일. 사용 방식은 아래 참고 | RRSI 코드, 제안자 프롬프트에 들어감 |
| critic (결정론적 precheck + LLM 검토, 평가 전 실행) | `harness-critic` 정책 | 규칙을 Claude Code에 맞게 고침. `policy_tampering`, `redrawn_rejection` 추가 | RRSI 코드, Claude Code용 패턴과 설명 |
| 거부 후 제한된 수리 반복 | deny 사유 → 에이전트 수정, `max_repairs` | 비슷함. 한도를 넘기면 버리지 않고 사용자에게 넘김 | RRSI 코드 (`repair_rounds`) |
| 노이즈 대역 δ 추정 | `baseline --k` → z·sd·√(2/k) | 비슷함. RRSI의 "기준 반복 평가" 방식과 같고, bootstrap은 없음 | RRSI 코드 (bootstrap 또는 반복 평가) |
| prune set과 탐색 지시를 제안자에게 **강제** | critic에 참고 정보로만 전달 | **약함**. 예약 슬롯 같은 강제 장치 없음 | RRSI 코드 (예약 슬롯) |
| 코사인으로 줄어드는 편집 예산 b_t | 고정된 `max_pending` | **다름**. 독립 편집이 아니라 도구 호출 수를 셈 | RRSI 코드 |
| 선언된 컴포넌트를 diff 근거로 검증 (`has_evidence`) | 경로 힌트 + LLM 분류 | **부분적** | RRSI 코드 |
| 라운드당 후보 m개 병렬 평가 후 argmax | 없음 (한 번에 묶음 하나) | **없음** | RRSI 코드 |
| 제안자, 분석기(analyst), 다이제스터(실패 trace 분석) | 없음 (Claude Code 자신이나 사용자가 제안자) | **없음** | RRSI 코드, 역할은 `claude -p`로 실행 |
| worktree 기반 후보 격리 | 없음 (실제 파일 + 스냅샷) | **없음** | RRSI 코드 |

정리하면 `rrsi-policy`는 RRSI를 그대로 돌리는 것이 아닙니다. RRSI의 **선택 쪽 정규화**(노이즈
하한, 비용 규칙, novelty)와 **critic**은 실제로 동작하지만, **제안 쪽 정규화**(줄어드는 예산, 강제
탐색, 이력 기반 제안)는 critic에 참고 정보를 주는 수준입니다. `rrsi-evolve`는 양쪽을 다 돌립니다.
여기서 다른 것은 알고리즘이 아니라 환경입니다. 컨테이너 대신 bwrap 샌드박스(없으면 호스트)에서
도는 Claude Code 세션이고, 헌법은 Claude Code용으로 썼습니다.

## 다른 곳에서 쓰기

```bash
git diff --cached -- CLAUDE.md | rrsi-policy check --policy harness-critic --file CLAUDE.md   # 0 pass / 2 deny / 3 ask
rrsi-policy check --policy secrets --command "export TOKEN=..."
rrsi-policy hook --policy harness-critic --dry-run < event.json                            # LLM 없이 라우팅·precheck만
```

## rrsi-policy의 한계

- Bash 경로는 정규식으로 잡기 때문에 완전하지 않습니다(예: 스크립트 내부에서 파일을 쓰는 경우).
  보안 경계가 아니라 가드레일입니다.
- Bash로 바꾼 파일은 스냅샷이 없어 `revert`로 복원되지 않습니다.
- 하네스 편집 한 번마다 critic을 기다리는 시간이 15~20초 걸립니다.
- 측정 품질은 eval 명령의 품질과 k에 달려 있습니다. k=1로는 δ를 추정할 수 없어 `--delta`를 직접
  줘야 합니다.
- critic은 LLM이라 오판할 수 있습니다. 거부 사유가 틀렸으면 `max_repairs` 이후 사용자에게 넘어갑니다.
- 실제 `claude -p` 판정은 아직 몇 건으로만 확인했습니다. 이 플러그인이 실사용에서 결과를 좋게
  만드는지는 아직 측정되지 않았습니다.

## 테스트

```bash
python3 -m pytest tests     # 가짜 실행 파일(tests/fake_*.py)이 claude -p를 대신함
```

## 라이선스

Apache-2.0([LICENSE](LICENSE)). 일부는 [google-research/rrsi](https://github.com/google-research/rrsi)
(Copyright 2026 Google LLC, Apache-2.0)에서 가져와 고쳤고, 목록은 [NOTICE](NOTICE)에 있습니다.
Google과 관계없는 개인 프로젝트이며 Google이 보증하지 않습니다.
