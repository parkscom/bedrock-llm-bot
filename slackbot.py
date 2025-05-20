"""
# 로컬 테스트용 환경 변수 예시 (실제 값으로 대체 필요)
export BEDROCK_MODEL_ID=anthropic.claude-3-sonnet-20240229-v1:0 
export SLACK_BOT_TOKEN=xoxb-YOUR_SLACK_BOT_TOKEN
export SLACK_SIGNING_SECRET=YOUR_SLACK_SIGNING_SECRET

export AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="YOUR_AWS_SECRET_ACCESS_KEY"
export AWS_REGION='ap-northeast-2' # 예시 리전

ngrok http 3000

"""


# -*- coding: utf-8 -*-
import os
import logging
import json
import time # 시간 측정을 위해 추가
import boto3 # AWS SDK for Python
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk.errors import SlackApiError # Slack API 에러 처리를 위해 추가
import threading # 로컬 서버 비동기 처리를 위해 추가
from collections import deque # 이벤트 ID 중복 제거를 위해 추가

# --- 로깅 설정 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# --- 이벤트 ID 중복 처리 설정 ---
MAX_EVENT_ID_MEMORY = 200 # 최근 N개의 이벤트 ID만 기억 (메모리 관리)
PROCESSED_EVENT_IDS = deque(maxlen=MAX_EVENT_ID_MEMORY)

# --- AWS 및 Bedrock 설정 ---
try:
    bedrock_model_id = os.environ['BEDROCK_MODEL_ID']
    aws_region = os.environ.get('AWS_REGION', 'ap-northeast-2') 
    bedrock_runtime = boto3.client(
        service_name='bedrock-runtime',
        region_name=aws_region
    )
    logger.info(f"Bedrock Runtime 클라이언트 생성 완료 (모델: {bedrock_model_id}, 리전: {aws_region})")
except KeyError as e:
    logger.error(f"필수 환경 변수 누락: {e}. BEDROCK_MODEL_ID를 확인하세요.")
    raise e
except Exception as e:
    logger.error(f"Bedrock 클라이언트 생성 실패: {e}", exc_info=True)
    raise e


# --- Slack 앱 초기화 ---
try:
    app = App(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
        process_before_response=True # 실제 Lambda 환경 및 로컬 비동기 처리와 일관성 유지
    )
    logger.info("Slack App 초기화 완료.")
except KeyError as e:
    logger.error(f"필수 환경 변수 누락: {e}. SLACK_BOT_TOKEN 또는 SLACK_SIGNING_SECRET을 확인하세요.")
    raise e

# --- Helper 함수: Bedrock LLM 호출 ---
def invoke_llm(prompt: str) -> str:
    """
    주어진 프롬프트를 사용하여 Bedrock LLM을 호출하고 응답 텍스트를 반환합니다.
    Claude 3 Sonnet (Messages API) 기준입니다.
    """
    messages = [
        {"role": "user", "content": prompt} 
    ]

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31", 
        "max_tokens": 1024, 
        "messages": messages,
        "temperature": 0.7, 
        "top_p": 0.9,       
    })

    try:
        logger.info(f"Bedrock 모델 ({bedrock_model_id}) 호출 시작")
        logger.debug(f"Bedrock 호출 프롬프트 (일부): {prompt[:250]}...") 
        response = bedrock_runtime.invoke_model(
            body=body,
            modelId=bedrock_model_id,
            accept='application/json',
            contentType='application/json'
        )
        logger.info("Bedrock 모델 호출 성공")
        response_body = json.loads(response.get('body').read())
        
        if response_body.get("content") and isinstance(response_body["content"], list) and len(response_body["content"]) > 0:
            llm_response = response_body['content'][0].get('text', '죄송합니다, 답변 내용이 비어있습니다.')
        else:
            llm_response = '죄송합니다, 예상치 못한 응답 형식입니다.'
            logger.error(f"Bedrock 예상치 못한 응답 형식: {response_body}")

        logger.info(f"Bedrock 응답 수신 (일부): {llm_response[:100]}...")
        return llm_response.strip()
    except Exception as e:
        logger.error(f"Bedrock 모델 호출 중 오류 발생: {e}", exc_info=True)
        return "죄송합니다, 답변을 생성하는 중 오류가 발생했습니다. 😥"

# --- Helper 함수: 스레드 대화 내용 JSON 타임라인 포맷팅 ---
def format_conversation_to_json_timeline(messages: list, bot_user_id: str) -> str:
    timeline = []
    for msg in messages:
        text = msg.get("text", "").strip()
        user_id_from_msg = msg.get("user")
        bot_id_from_msg = msg.get("bot_id") 

        if user_id_from_msg == bot_user_id or bot_id_from_msg:
            speaker_from = "bot"
        elif user_id_from_msg:
            speaker_from = f"<@{user_id_from_msg}>" 
        else:
            logger.debug(f"발신자 정보 없는 메시지 건너뜀: {msg.get('ts')}")
            continue

        if bot_user_id and f"<@{bot_user_id}>" in text:
             text = text.replace(f"<@{bot_user_id}>", "").strip()

        if text: 
             timeline.append({"from": speaker_from, "message": text})
        else:
             logger.debug(f"내용 없는 메시지 건너뜀: {msg.get('ts')}")

    return json.dumps(timeline, ensure_ascii=False, indent=2) 

# --- Helper 함수: LLM 프롬프트 생성 ---
def create_llm_prompt(system_prompt_text: str, conversation_json: str, latest_query_text_from_event: str) -> str:
    """
    시스템 프롬프트, JSON 타임라인, 최신 질문을 기반으로 LLM 프롬프트를 생성합니다.
    """
    try:
        timeline_data = json.loads(conversation_json)
        is_empty_or_invalid_timeline = not isinstance(timeline_data, list) or not timeline_data
    except json.JSONDecodeError:
        is_empty_or_invalid_timeline = True 

    if is_empty_or_invalid_timeline:
        if conversation_json == "[]": 
             user_interaction_part = f"다음 질문에 대해 답변해주세요:\n\n{latest_query_text_from_event}"
        else: 
             user_interaction_part = f"다음은 대화 내용입니다:\n```json\n{conversation_json}\n```\n위 대화 타임라인의 마지막 메시지에 대해 답변해주세요."
    else:
        user_interaction_part = f"다음은 대화 내용입니다:\n```json\n{conversation_json}\n```\n위 대화 타임라인의 마지막 메시지에 대해 답변해주세요."
    
    full_prompt = f"{system_prompt_text}\n\n{user_interaction_part}"
    logger.debug(f"생성된 전체 LLM 프롬프트 (일부): {full_prompt[:200]}...")
    return full_prompt


# --- Slack 이벤트 핸들러 ---
@app.event("app_mention")
def handle_app_mention_events(body, say, logger, client):
    # `body`는 SlackRequestHandler가 파싱한 Slack 이벤트 페이로드 자체입니다.
    # `event_id`는 이 `body`의 최상위 레벨에 있습니다.
    event_id = body.get("event_id")
    if event_id:
        if event_id in PROCESSED_EVENT_IDS:
            logger.warning(f"중복 이벤트 수신 및 무시: {event_id}")
            return # 이미 처리된 이벤트이므로 여기서 중단
        PROCESSED_EVENT_IDS.append(event_id) # 새 이벤트 ID 추가 (deque는 오래된 ID 자동 제거)
        logger.info(f"새 이벤트 처리 시작: {event_id} (PROCESSED_EVENT_IDS 크기: {len(PROCESSED_EVENT_IDS)})")
    else:
        logger.warning("요청에서 event_id를 찾을 수 없습니다. 중복 처리 방지가 작동하지 않을 수 있습니다.")

    # 실제 이벤트 내용은 `body['event']` 안에 있습니다.
    actual_event_payload = body.get("event", {})
    user_id = actual_event_payload.get("user")
    text = actual_event_payload.get("text", "")
    channel_id = actual_event_payload.get("channel")
    thread_ts = actual_event_payload.get("thread_ts") 
    event_ts = actual_event_payload.get("ts") 

    logger.info(f"멘션 수신 (이벤트 ID: {event_id}): 사용자={user_id}, 채널={channel_id}, 스레드={thread_ts}, 원본 메시지 TS={event_ts}, 내용='{text}'")

    target_thread_ts_for_all_replies = thread_ts if thread_ts else event_ts
    waiting_message_ts = None 

    try:
        # 봇 ID는 `body['authorizations']` 또는 `body['authed_users']` 등에서 가져올 수 있습니다.
        # `slack_bolt`가 `client` 객체를 통해 `auth.test()`를 호출하여 현재 봇 ID를 가져오는 것이 더 안정적일 수 있습니다.
        # 여기서는 이전 방식대로 `authorizations`를 사용합니다.
        auth_info_list = body.get("authorizations")
        bot_user_id = ""
        if auth_info_list and isinstance(auth_info_list, list) and len(auth_info_list) > 0:
            bot_user_id = auth_info_list[0].get("user_id", "")
        
        if not bot_user_id:
            # client.auth_test()를 사용한 봇 ID 획득 시도 (더 안정적)
            try:
                auth_test_result = client.auth_test()
                bot_user_id = auth_test_result.get("user_id")
                logger.info(f"client.auth_test()를 통해 봇 ID 획득: {bot_user_id}")
            except Exception as auth_e:
                logger.error(f"client.auth_test() 호출 실패: {auth_e}")
                # 이 경우에도 처리를 중단하거나, bot_user_id 없이 진행 (멘션 제거 등에 영향)

        if not bot_user_id:
            logger.error("봇 ID를 가져올 수 없습니다. authorizations 블록 또는 auth.test() 결과를 확인해주세요.")
            say(text="죄송합니다, 봇 설정을 초기화하는 중 오류가 발생했습니다. (봇 ID 확인 불가)", thread_ts=target_thread_ts_for_all_replies)
            return

        # 0. 시스템 프롬프트 로드
        system_prompt_text = ""
        prompt_filename = f"system_prompt_{bot_user_id}.txt"
        try:
            # Lambda 환경에서는 /var/task/ 가 현재 작업 디렉토리
            # 로컬에서는 스크립트 실행 위치 기준
            # 파일 경로를 절대 경로로 지정하거나, Lambda Layer를 사용하는 것이 더 안정적일 수 있습니다.
            # 여기서는 현재 작업 디렉토리를 기준으로 파일을 찾습니다.
            base_path = os.path.dirname(os.path.abspath(__file__)) if "__file__" in locals() else os.getcwd()
            full_prompt_path = os.path.join(base_path, prompt_filename)
            
            with open(full_prompt_path, 'r', encoding='utf-8') as f:
                system_prompt_text = f.read().strip()
            if not system_prompt_text:
                logger.error(f"시스템 프롬프트 파일 '{full_prompt_path}'이 비어있습니다.")
                say(text=f"죄송합니다, <@{user_id}>님. 봇의 설정에 문제가 있습니다 (프롬프트 내용 없음). 관리자에게 문의해주세요.", thread_ts=target_thread_ts_for_all_replies)
                return
            logger.info(f"시스템 프롬프트 로드 완료: {full_prompt_path}")
        except FileNotFoundError:
            logger.error(f"시스템 프롬프트 파일 '{full_prompt_path}'을 찾을 수 없습니다.")
            say(text=f"죄송합니다, <@{user_id}>님. 봇의 시스템 프롬프트가 설정되어 있지 않습니다. 관리자에게 문의해주세요.", thread_ts=target_thread_ts_for_all_replies)
            return
        except Exception as e:
            logger.error(f"시스템 프롬프트 파일 로딩 중 오류 발생 ({prompt_filename}): {e}", exc_info=True)
            say(text=f"죄송합니다, <@{user_id}>님. 봇 설정을 불러오는 중 오류가 발생했습니다. 관리자에게 문의해주세요.", thread_ts=target_thread_ts_for_all_replies)
            return

        if bot_user_id and f"<@{bot_user_id}>" in text:
             user_query = text.replace(f"<@{bot_user_id}>", "").strip()
        else:
             parts = text.split(" ", 1)
             user_query = parts[1].strip() if len(parts) > 1 else ""

        if not user_query:
            logger.warning("사용자 질문 내용이 비어있습니다.")
            say(text=f"<@{user_id}>님, 질문 내용을 입력해주세요.", thread_ts=target_thread_ts_for_all_replies)
            return

        logger.info(f"추출된 사용자 질문: '{user_query}'")

        try:
            waiting_message_response = client.chat_postMessage(
                channel=channel_id,
                text="잠시만 기다려주세요, 답변을 생성하고 있습니다... ⏳",
                thread_ts=target_thread_ts_for_all_replies
            )
            waiting_message_ts = waiting_message_response.get("ts")
            if waiting_message_ts:
                logger.info(f"임시 메시지 전송 완료 (ts: {waiting_message_ts})")
            else:
                logger.warning("임시 메시지 전송 후 ts를 받지 못했습니다.")
        except SlackApiError as e:
            logger.error(f"임시 메시지 전송 실패: {e}")

        conversation_json = "[]" 
        messages_from_slack = [] 

        if thread_ts: 
            logger.info(f"스레드({thread_ts}) 내 질문입니다. 대화 기록을 가져옵니다.")
            try:
                result = client.conversations_replies(
                    channel=channel_id,
                    ts=thread_ts, 
                    limit=20 
                )
                messages_from_slack = result.get("messages", [])

                if messages_from_slack:
                     logger.info(f"{len(messages_from_slack)}개의 메시지를 스레드에서 가져왔습니다.")
                     conversation_json = format_conversation_to_json_timeline(messages_from_slack, bot_user_id)
                     logger.debug(f"생성된 JSON 타임라인 (스레드 기록):\n{conversation_json}")
                else:
                     logger.info("스레드에서 메시지를 가져오지 못했거나 메시지가 없습니다. 현재 질문만 사용합니다.")
                     current_message_timeline = [{"from": f"<@{user_id}>", "message": user_query}]
                     conversation_json = json.dumps(current_message_timeline, ensure_ascii=False, indent=2)

            except SlackApiError as e:
                error_target_ts = thread_ts 
                if e.response and e.response.get("error") == "missing_scope":
                     logger.error(f"Slack API 권한 부족 오류: {e.response}. 'conversations:history' 권한이 필요합니다.")
                     say(text=f"죄송합니다, <@{user_id}>님. 이전 대화 내용을 가져오려면 Slack 앱에 'conversations:history' 권한이 필요합니다. 앱 설정을 확인해주세요.", thread_ts=error_target_ts)
                else:
                     logger.error(f"Slack API 오류 (conversations.replies): {e.response['error'] if e.response else e}")
                     say(text=f"죄송합니다, <@{user_id}>님. 이전 대화 내용을 가져오는 중 오류가 발생했습니다.", thread_ts=error_target_ts)
                if waiting_message_ts:
                    try: client.chat_delete(channel=channel_id, ts=waiting_message_ts); logger.info(f"오류로 임시 메시지 삭제(ts: {waiting_message_ts})")
                    except Exception as del_e: logger.error(f"임시 메시지 삭제 실패: {del_e}")
                return 
            except Exception as e:
                 error_target_ts = thread_ts
                 logger.error(f"스레드 기록 처리 중 예외 발생: {e}", exc_info=True)
                 say(text=f"죄송합니다, <@{user_id}>님. 이전 대화 내용을 처리하는 중 오류가 발생했습니다.", thread_ts=error_target_ts)
                 if waiting_message_ts:
                    try: client.chat_delete(channel=channel_id, ts=waiting_message_ts); logger.info(f"오류로 임시 메시지 삭제(ts: {waiting_message_ts})")
                    except Exception as del_e: logger.error(f"임시 메시지 삭제 실패: {del_e}")
                 return
        else: 
             logger.info("새로운 질문 스레드입니다. 현재 메시지를 타임라인에 포함합니다.")
             current_message_timeline = [{"from": f"<@{user_id}>", "message": user_query}]
             conversation_json = json.dumps(current_message_timeline, ensure_ascii=False, indent=2)
             logger.debug(f"생성된 JSON 타임라인 (현재 메시지):\n{conversation_json}")

        prompt_for_llm = create_llm_prompt(system_prompt_text, conversation_json, user_query)

        start_time = time.time()
        llm_response = invoke_llm(prompt_for_llm)
        end_time = time.time()
        llm_duration = end_time - start_time
        logger.info(f"LLM 답변 생성 시간: {llm_duration:.2f}초")

        say(text=llm_response, thread_ts=target_thread_ts_for_all_replies)
        logger.info(f"LLM 응답 전송 완료 (스레드: {target_thread_ts_for_all_replies})")

        if waiting_message_ts:
            try:
                client.chat_delete(channel=channel_id, ts=waiting_message_ts)
                logger.info(f"임시 메시지(ts: {waiting_message_ts}) 삭제 완료")
            except SlackApiError as e:
                logger.error(f"임시 메시지 삭제 실패: {e}")
        
    except Exception as e:
        logger.error(f"이벤트 처리 중 예상치 못한 오류 발생 (이벤트 ID: {event_id}): {e}", exc_info=True)
        try:
            say(text=f"죄송합니다, <@{user_id}>님. 요청 처리 중 오류가 발생했습니다. 😥", thread_ts=target_thread_ts_for_all_replies)
        except Exception as notify_error:
            logger.error(f"오류 알림 메시지 전송 실패: {notify_error}", exc_info=True)
        
        if waiting_message_ts:
            try:
                client.chat_delete(channel=channel_id, ts=waiting_message_ts)
                logger.info(f"최종 예외 처리 중 임시 메시지(ts: {waiting_message_ts}) 삭제 시도 (이벤트 ID: {event_id})")
            except SlackApiError as delete_e:
                logger.error(f"최종 예외 처리 중 임시 메시지 삭제 실패: {delete_e}")


# --- AWS Lambda 핸들러 (Lambda 배포 시 사용) ---
def lambda_handler(event, context):
    logger.info("Lambda 핸들러 시작")
    slack_handler = SlackRequestHandler(app=app)
    # Lambda 이벤트 전체를 로깅하면 민감 정보가 포함될 수 있으므로, 필요한 부분만 로깅하거나 크기 제한
    # logger.debug(f"Lambda 이벤트 수신 (일부): {str(event)[:500]}") 
    if isinstance(event.get("body"), str):
        try:
            body_json = json.loads(event["body"])
            logger.info(f"Lambda 수신 이벤트 ID: {body_json.get('event_id')}, 타입: {body_json.get('type')}/{body_json.get('event',{}).get('type')}")
        except json.JSONDecodeError:
            logger.warning("Lambda 이벤트 body가 JSON 형식이 아닙니다.")
    
    return slack_handler.handle(event, context)

# --- 로컬 개발 서버 실행 (로컬 테스트 시 사용 - Lambda 시뮬레이션) ---
if __name__ == "__main__":
    from http.server import HTTPServer, BaseHTTPRequestHandler

    # 실제 lambda_handler 로직을 실행하는 함수
    def process_lambda_request(event_data, context_data):
        try:
            # 로깅 포맷을 스레드 ID 포함하도록 변경 (선택 사항)
            # current_thread = threading.current_thread()
            # logger.info(f"백그라운드 스레드 ({current_thread.name}): lambda_handler 호출 시작")
            logger.info(f"백그라운드 스레드: lambda_handler 호출 시작")
            lambda_handler(event_data, context_data)
            logger.info(f"백그라운드 스레드: lambda_handler 처리 완료")
        except Exception as e:
            logger.error(f"백그라운드 스레드: lambda_handler 처리 중 오류: {e}", exc_info=True)

    class LocalSlackRequestHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get('Content-Length', 0))
            request_body_bytes = self.rfile.read(content_length)
            request_body_str = request_body_bytes.decode('utf-8')

            # 요청 바디 로깅 시 주의 (민감 정보 가능성)
            # logger.debug(f"로컬 서버: 수신된 요청 바디 전체: {request_body_str}")
            try:
                parsed_body_for_log = json.loads(request_body_str)
                log_event_id = parsed_body_for_log.get('event_id', 'N/A')
                log_event_type = parsed_body_for_log.get('event', {}).get('type', 'N/A')
                logger.info(f"로컬 서버: POST 요청 수신 ({self.path}), 이벤트 ID: {log_event_id}, 이벤트 타입: {log_event_type}")
            except json.JSONDecodeError:
                logger.info(f"로컬 서버: POST 요청 수신 ({self.path}), 바디 파싱 불가")


            lambda_event = {
                "body": request_body_str, # SlackRequestHandler는 문자열 body를 기대함
                "headers": dict(self.headers),
                "httpMethod": "POST",
                "path": self.path,
                "isBase64Encoded": False,
                "requestContext": {
                    "http": {
                        "method": "POST",
                        "path": self.path,
                        "protocol": "HTTP/1.1",
                        "sourceIp": self.client_address[0],
                        "userAgent": self.headers.get('User-Agent')
                    },
                    "requestId": f"local-req-{os.urandom(8).hex()}",
                    "stage": "local",
                }
            }

            class DummyContext:
                def __init__(self):
                    self.function_name = "local-slack-bot-sim"
                    self.aws_request_id = f"local-aws-req-{os.urandom(8).hex()}"
                    self.invoked_function_arn = "arn:aws:lambda:local:123456789012:function:local-slack-bot-sim"
                    self.memory_limit_in_mb = 128
                    self._start_time = time.time()
                    self._max_duration_ms = 300000 # 예: 5분
                def get_remaining_time_in_millis(self):
                    elapsed_ms = (time.time() - self._start_time) * 1000
                    return max(0, self._max_duration_ms - int(elapsed_ms))

            lambda_context = DummyContext()

            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'') 
                logger.info(f"로컬 서버: 즉시 200 OK 응답 전송 완료 (이벤트 ID: {log_event_id}).")
            except Exception as e:
                logger.error(f"로컬 서버: 즉시 200 OK 응답 전송 중 오류: {e}", exc_info=True)
                return

            thread = threading.Thread(target=process_lambda_request, args=(lambda_event, lambda_context))
            thread.daemon = True 
            thread.start()
            logger.info(f"로컬 서버: lambda_handler를 백그라운드 스레드에서 실행 시작 (이벤트 ID: {log_event_id}).")


    host = 'localhost'
    port = int(os.environ.get("PORT", 3000))
    server_address = (host, port)

    logger.info("로컬 실행을 위한 환경 변수 확인:")
    logger.info(f"  SLACK_BOT_TOKEN: {'설정됨' if os.environ.get('SLACK_BOT_TOKEN') else '설정 안됨!'}")
    logger.info(f"  SLACK_SIGNING_SECRET: {'설정됨' if os.environ.get('SLACK_SIGNING_SECRET') else '설정 안됨!'}")
    logger.info(f"  BEDROCK_MODEL_ID: {os.environ.get('BEDROCK_MODEL_ID', '설정 안됨!')}")
    logger.info(f"  AWS_REGION: {os.environ.get('AWS_REGION', 'ap-northeast-2')}") 

    logger.info(f"로컬 Lambda 시뮬레이션 서버 시작 (http://{host}:{port})")
    logger.info(f"시스템 프롬프트 파일 예시: system_prompt_YOUR_BOT_ID.txt (봇 ID는 로그에서 확인 가능)")
    logger.info("Slack 앱의 Request URL을 ngrok URL로 설정하세요.")
    
    httpd = HTTPServer(server_address, LocalSlackRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("로컬 서버 종료 중...")
    finally:
        httpd.server_close()
        logger.info("로컬 서버가 성공적으로 종료되었습니다.")
