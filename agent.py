import asyncio
import logging
import argparse
from dotenv import load_dotenv
import json
import os
from time import perf_counter
from typing import Annotated, Optional, Sequence
from livekit import rtc, api
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, silero


"""LiveKit outbound caller agent.

Provides a small CLI for development utilities and runs the LiveKit worker.
"""


# load environment variables for local dev if present
load_dotenv(dotenv_path=".env.local")


def _configure_logging(level: str = "INFO") -> None:
    """Configure root logging with a helpful format.

    Parameters
    ----------
    level: str
        Logging level name, e.g. "DEBUG", "INFO", "WARNING".
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    else:
        logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))


logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

REQUIRED_ENV_VARS = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "SIP_OUTBOUND_TRUNK_ID",
)


def validate_required_env() -> None:
    """Validate that all required environment variables are set.

    Raises
    ------
    ValueError
        If any required environment variable is missing or invalid.
    """
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
    if not trunk_id.startswith("ST_"):
        raise ValueError("SIP_OUTBOUND_TRUNK_ID must start with 'ST_'")


outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
_default_instructions = (
    "You are a scheduling assistant for a game development studio. Your interface with user will be voice."
    "You will be on a call with a customer who has an upcoming appointment. Your goal is to confirm the appointment details."
    "As a customer service representative, you will be polite and professional at all times. Allow user to end the conversation."
)


async def entrypoint(ctx: JobContext) -> None:
    global _default_instructions, outbound_trunk_id
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    user_identity = "phone_user"
    # the phone number to dial is provided in the job metadata
    phone_number = ctx.job.metadata
    logger.info(f"dialing {phone_number} to room {ctx.room.name}")

    # look up the user's phone number and appointment details
    instructions = (
        _default_instructions
        + "The customer's name is Kyle. His appointment is next Tuesday at 3pm."
    )

    # `create_sip_participant` starts dialing the user
    await ctx.api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=outbound_trunk_id,
            sip_call_to=phone_number,
            participant_identity=user_identity,
        )
    )

    # a participant is created as soon as we start dialing
    participant = await ctx.wait_for_participant(identity=user_identity)

    run_voice_pipeline_agent(ctx, participant, instructions)

    # in addition, you can monitor the call status separately
    start_time = perf_counter()
    while perf_counter() - start_time < 30:
        call_status = participant.attributes.get("sip.callStatus")
        if call_status == "active":
            logger.info("user has picked up")
            return
        elif call_status == "automation":
            # if DTMF is used in the `sip_call_to` number, typically used to dial
            # an extension or enter a PIN.
            # during DTMF dialing, the participant will be in the "automation" state
            pass
        elif participant.disconnect_reason == rtc.DisconnectReason.USER_REJECTED:
            logger.info("user rejected the call, exiting job")
            break
        elif participant.disconnect_reason == rtc.DisconnectReason.USER_UNAVAILABLE:
            logger.info("user did not pick up, exiting job")
            break
        await asyncio.sleep(0.1)

    logger.info("session timed out, exiting job")
    ctx.shutdown()


class CallActions(llm.FunctionContext):
    """
    Detect user intent and perform actions
    """

    def __init__(
        self, *, api: api.LiveKitAPI, participant: rtc.RemoteParticipant, room: rtc.Room
    ):
        super().__init__()
        self.api = api
        self.participant = participant
        self.room = room

    async def hangup(self) -> None:
        try:
            await self.api.room.remove_participant(
                api.RoomParticipantIdentity(
                    room=self.room.name,
                    identity=self.participant.identity,
                )
            )
        except Exception as e:
            # it's possible that the user has already hung up, this error can be ignored
            logger.info(f"received error while ending call: {e}")

    @llm.ai_callable()
    async def end_call(self) -> None:
        """Called when the user wants to end the call"""
        logger.info(f"ending the call for {self.participant.identity}")
        await self.hangup()

    @llm.ai_callable()
    async def look_up_availability(
        self,
        date: Annotated[str, "The date of the appointment to check availability for"],
    ) -> str:
        """Called when the user asks about alternative appointment availability"""
        logger.info(
            f"looking up availability for {self.participant.identity} on {date}"
        )
        await asyncio.sleep(3)
        return json.dumps(
            {
                "available_times": ["1pm", "2pm", "3pm"],
            }
        )

    @llm.ai_callable()
    async def confirm_appointment(
        self,
        date: Annotated[str, "date of the appointment"],
        time: Annotated[str, "time of the appointment"],
    ) -> str:
        """Called when the user confirms their appointment on a specific date. Use this tool only when they are certain about the date and time."""
        logger.info(
            f"confirming appointment for {self.participant.identity} on {date} at {time}"
        )
        return "reservation confirmed"

    @llm.ai_callable()
    async def detected_answering_machine(self) -> None:
        """Called when the call reaches voicemail. Use this tool AFTER you hear the voicemail greeting"""
        logger.info(f"detected answering machine for {self.participant.identity}")
        await self.hangup()


def run_voice_pipeline_agent(
    ctx: JobContext, participant: rtc.RemoteParticipant, instructions: str
) -> None:
    logger.info("starting voice pipeline agent")

    initial_ctx = llm.ChatContext().append(
        role="system",
        text=instructions,
    )

    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-2-phonecall"),
        llm=openai.LLM(),
        tts=openai.TTS(),
        chat_ctx=initial_ctx,
        fnc_ctx=CallActions(api=ctx.api, participant=participant, room=ctx.room),
    )

    agent.start(ctx.room, participant)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


def download_files() -> None:
    """Download/cache any models or assets needed for offline/first-run.

    Currently loads Silero VAD to populate the local cache.
    """
    logger.info("downloading/caching required models (silero VAD)...")
    _ = silero.VAD.load()
    logger.info("download complete")


def healthcheck() -> None:
    """Quick environment validation and dependency sanity check."""
    validate_required_env()
    logger.info("environment variables validated")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveKit outbound caller agent")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("dev", help="Run the agent worker in development mode")
    sub.add_parser("healthcheck", help="Validate environment configuration")
    sub.add_parser("download-files", help="Download/cache required models")
    sub.add_parser("prewarm", help="Preload models to reduce first-run latency")

    return parser.parse_args(argv)


def _run_dev() -> None:
    validate_required_env()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
            prewarm_fnc=prewarm,
        )
    )


if __name__ == "__main__":
    args = _parse_args()
    _configure_logging(args.log_level)
    if args.command == "dev":
        _run_dev()
    elif args.command == "healthcheck":
        healthcheck()
    elif args.command == "download-files":
        download_files()
    elif args.command == "prewarm":
        # Simply call the same download routine; actual prewarm happens inside worker via prewarm_fnc
        download_files()
    else:
        raise SystemExit(2)