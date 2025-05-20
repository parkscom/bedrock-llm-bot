# Slack 챗봇 with AWS Bedrock LLM (Gemini & 프롬프트 기반 개발)

## 1. 프로젝트 개요 및 목적

본 프로젝트는 Slack에서 사용자와 자연스럽게 대화하며 질문에 답변하는 AI 챗봇을 구축하는 것을 목표로 합니다.

### 개발 배경 및 특징

* Google의 Gemini 모델을 활용하여 생성된 코드
* 대규모 언어 모델(LLM)과 프롬프트 기반 개발(Prompt-driven Development) 방식을 시험
* **Vibe Coding** 방식 탐색

> **참고:** 주요 프롬프트 예시는 `generate_prompt.txt` 파일 참조 (실제 개발 시 별도 기록/관리 필요)

### 듀얼 실행 환경 지원

* **로컬 PC**: 개발 및 테스트 환경
* **AWS Lambda**: 서버리스 운영 환경

### 주요 기능

1. **Slack 멘션 기반 응답**: 봇이 멘션된 질문에 응답
2. **스레드 기반 대화**: 기존 스레드에 댓글 형태로 답변
3. **AWS Bedrock LLM 연동**: Amazon Bedrock LLM 호출
4. **시스템 프롬프트 외부화**: 외부 텍스트 파일에서 로드하여 유연성 확보
5. **응답 대기 메시지 & 처리 시간 로깅**
6. **이벤트 중복 처리 방지**: 재시도 등 중복 수신 방어 로직 포함

---

## 2. 실행 환경

* **프로그래밍 언어**: Python 3.9 이상
* **주요 라이브러리**:

  * `slack_bolt`: Slack 앱 개발
  * `boto3`: AWS 서비스 연동
  * 기타: `os`, `logging`, `json`, `time`, `threading`, `collections`
* **필수 외부 서비스**:

  * Slack Workspace 및 앱
  * AWS (Bedrock, Lambda, CloudWatch Logs)
  * (로컬 테스트) ngrok

---

## 3. 로컬 환경에서 실행 및 테스트 방법

코드 변경 즉시 테스트 가능하도록 로컬 PC 환경에서 실행하며, AWS Lambda 환경을 시뮬레이션합니다.

### 3.1 사전 준비

1. **Python & pip 설치**: Python 3.9+ 및 pip
2. **라이브러리 설치**:

   ```bash
   pip install slack_bolt boto3
   ```
3. **AWS CLI 설정 (권장)**:

   ```bash
   aws configure
   ```

   또는 환경 변수 직접 설정:

   ```bash
   export AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID"
   export AWS_SECRET_ACCESS_KEY="YOUR_AWS_SECRET_ACCESS_KEY"
   export AWS_REGION="ap-northeast-2"
   ```
4. **ngrok 설치**: [ngrok 공식 웹사이트](https://ngrok.com)에서 다운로드
5. **Slack 앱 설정**:

   * **Basic Information**: Signing Secret 확인
   * **OAuth & Permissions**:

     * `app_mentions:read`
     * `chat:write`
     * `conversations:history`
   * **Event Subscriptions**:

     * Enable Events On
     * Subscribe to `app_mention`
     * Request URL: ngrok 공개 URL

### 3.2 환경 변수 설정

```bash
# Slack
export SLACK_BOT_TOKEN="xoxb-YOUR_SLACK_BOT_TOKEN"
export SLACK_SIGNING_SECRET="YOUR_SLACK_SIGNING_SECRET"

# AWS Bedrock
export BEDROCK_MODEL_ID="anthropic.claude-3-sonnet-20240229-v1:0"
export AWS_REGION="ap-northeast-2"
```

(선택) AWS 자격 증명:

```bash
# export AWS_ACCESS_KEY_ID="..."
# export AWS_SECRET_ACCESS_KEY="..."
```

### 3.3 시스템 프롬프트 파일 준비

1. 봇 User ID 확인 (예: `U012ABCDEF`)
2. 프로젝트 디렉터리에 `system_prompt_<봇UserID>.txt` 생성

```text
당신은 Slack 봇 개발을 전문으로 도와주는 친절하고 유능한 AI 어시스턴트입니다.
사용자의 질문에 대해 Slack 봇 아키텍처, API 사용법, 이벤트 처리 등과 관련된 답변을 제공해주세요.
답변은 항상 명확하고 이해하기 쉽게 한국어로 작성하며, 일반 텍스트 대화 형식이어야 합니다.
```

### 3.4 로컬 서버 및 ngrok 실행

```bash
# 로컬 서버 실행
python your_script_name.py

# ngrok 실행 (기본 포트 3000)
ngrok http 3000
```

* ngrok이 제공한 HTTPS URL을 Slack 앱 Request URL에 입력
* Verified 상태 확인
* 필요 시 앱 재설치

### 3.5 테스트

* Slack 워크스페이스에서 봇 멘션
* 로컬 터미널 로그로 처리 과정 확인

---

## 4. AWS Lambda로 배포 및 구성

로컬과 동일한 코드로 AWS Lambda에 배포하여 운영 환경 구성

### 4.1 IAM 권한 설정 (Bedrock 접근)

Lambda 실행 역할에 다음 정책 추가:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:ap-northeast-2::foundation-model/YOUR_BEDROCK_MODEL_ID"
    }
  ]
}
```

> **권장:** 최소 권한 원칙 준수

### 4.2 배포 패키지 준비

```bash
# 라이브러리 패키징
pip install slack_bolt boto3 -t .
# ZIP 파일 생성
zip -r slack_chatbot_lambda.zip .
```

### 4.3 Lambda 함수 생성 (AWS CLI 예시)

```bash
aws lambda create-function \
  --function-name your-slack-chatbot-function \
  --runtime python3.9 \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/YOUR_LAMBDA_ROLE \
  --handler your_script_name.lambda_handler \
  --zip-file fileb://slack_chatbot_lambda.zip \
  --timeout 60 \
  --memory-size 256 \
  --region ap-northeast-2 \
  --environment "Variables={SLACK_BOT_TOKEN=xoxb-...,SLACK_SIGNING_SECRET=...,BEDROCK_MODEL_ID=...,AWS_REGION=...}"
```

* API Gateway 트리거 설정: POST `/slack/events` 등
* Invoke URL을 Slack 앱 Request URL에 입력

### 4.5 테스트 및 모니터링

* Slack 멘션 테스트
* CloudWatch Logs에서 Lambda 실행 로그 확인

---

## 5. 주요 코드 구조

```python
# your_script_name.py

# 초기화: 환경 변수, 로거, Slack 앱, Bedrock 클라이언트, 이벤트 ID 저장소
# Helper 함수: invoke_llm, format_conversation_to_json_timeline, create_llm_prompt

@app.event("app_mention")
def handle_app_mention_events(...):
    # 시스템 프롬프트 로드
    # 중복 확인
    # 임시 메시지
    # LLM 호출
    # 응답 전송


def lambda_handler(event, context):
    # Lambda 진입점

if __name__ == "__main__":
    # 로컬 테스트용 HTTP 서버
```

## 6. 문제 해결 및 디버깅

* **환경 변수 & 시스템 프롬프트 파일** 확인
* **Slack 앱 권한 & Request URL** 검토
* **로컬 테스트**: ngrok 연결 및 터미널 로그
* **AWS Lambda**: IAM 역할, 환경 변수, 패키지 내용, CloudWatch Logs, Timeout/메모리 설정
* **이벤트 중복**: `PROCESSED_EVENT_IDS` 로직 및 비동기 처리 확인

---

이 README가 프로젝트 이해 및 설정에 도움이 되길 바랍니다.
