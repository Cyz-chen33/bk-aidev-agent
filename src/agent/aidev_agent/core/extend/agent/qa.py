# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.
"""

import enum
import json
import time
from collections import defaultdict, deque
from copy import deepcopy
from logging import getLogger
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union

from asgiref.sync import sync_to_async
from langchain.agents.format_scratchpad.tools import format_to_tool_messages
from langchain.tools.render import render_text_description_and_args
from langchain_community.adapters.openai import convert_dict_to_message, convert_message_to_dict
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.callbacks import Callbacks
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables.config import _set_config_context
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from aidev_agent.config import settings
from aidev_agent.core.agent.agents import (
    ACTION_INPUT_ERR_MSG,
    OUTPUT_PARSER_ERR_MSG,
    enhanced_format_log_to_str,
    get_beijing_now,
)
from aidev_agent.core.agent.multimodal import MultiToolCallCommonAgent, StructuredChatCommonAgent
from aidev_agent.core.utils.local import request_local
from aidev_agent.utils import Empty

from ..intent.intent_recognition import Decision, FineGrainedScoreType, IntentRecognition, IntentStatus
from ..intent.prompts import DEFAULT_QA_PROMPT_TEMPLATES
from ..intent.utils import (
    FINAL_ANSWER_PREFIXES,
    FINAL_ANSWER_SUFFIXES,
    conditional_dispatch_custom_event,
    deduplicate_knowledge_file_paths,
    deduplicate_tools,
    filter_and_select_topk,
    is_deepseek_r1_series_models,
    is_structured_data,
    query_clarification_enabled,
    support_multimodal,
)

_logger = getLogger(__name__)


class EventType(enum.Enum):
    LOADING = "loading"
    TEXT = "text"
    DONE = "done"
    ERROR = "error"
    REFERENCE_DOC = "reference_doc"
    THINK = "think"


class IntentRecognitionMixin(BaseModel):
    qa_prompt_templates: ClassVar[Dict[str, Any]] = DEFAULT_QA_PROMPT_TEMPLATES
    intent_recognition_instance: ClassVar[IntentRecognition] = IntentRecognition()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        parent_factory = getattr(super(cls, cls), "qa_prompt_templates", {})
        current_factory = getattr(cls, "qa_prompt_templates", {})
        cls.qa_prompt_templates = {**parent_factory, **current_factory}

    def intent_recognition_pipeline(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        kwargs: Any = None,
    ):
        if hasattr(request_local, "intent_recognition_results"):
            # 防止意图识别流程重复跑
            llm = request_local.intent_recognition_results["llm"]
            chat_prompt_template = request_local.intent_recognition_results["chat_prompt_template"]
            candidate_tools = request_local.intent_recognition_results["candidate_tools"]
            intermediate_steps = request_local.intent_recognition_results["intermediate_steps"]
            callbacks = request_local.intent_recognition_results["callbacks"]
            # 更新覆盖，而不是将整个kwargs替换，防止某些 key 可能被丢失了
            kwargs = {**kwargs, **request_local.intent_recognition_results["kwargs"]}
        else:
            if self.intent_recognition_kwargs:
                kwargs = {**kwargs, **self.intent_recognition_kwargs}
            (
                llm,
                chat_prompt_template,
                candidate_tools,
                intermediate_steps,
                callbacks,
                kwargs,
            ) = self.__class__.intent_recognition(
                self.llm,
                self.prefix,
                self.role_prompt or "",
                self.tools,
                intermediate_steps,
                callbacks,
                force_process_by_agent=False,
                **kwargs,
            )
            # NOTE: 需要格外注意此处的深拷贝和浅拷贝逻辑
            # 1. 对于 chat_prompt_template，candidate_tools 和 kwargs 这类希望只在首次调用 intent_recognition 时修改的
            # 需要进行深拷贝。
            # 2. 对于 intermediate_steps 这类在 agent 过程中需要不断修改的，需要进行浅拷贝，通过引用的方式，使得可以一直被根据需要修改。
            request_local.intent_recognition_results = {
                "llm": llm,
                "chat_prompt_template": deepcopy(chat_prompt_template),
                "candidate_tools": deepcopy(candidate_tools),
                # NOTE：intermediate_steps 在 agent 过程中需要一直能变，因此千万不能 deepcopy！
                "intermediate_steps": intermediate_steps,
                "callbacks": callbacks,
                "kwargs": deepcopy(kwargs),
            }

        # 根据 deepseek 官方建议 https://github.com/deepseek-ai/DeepSeek-R1?tab=readme-ov-file#usage-recommendations
        # deepseek-r1 系列模型需要避免使用 system prompt
        # 这里统一转一下（否则用户选择“预设角色”可能包含 system prompt）
        # NOTE: 虽然聊天窗侧统一支持了以下转换，但还需要支持插件侧使用，因此这里还是需要做下检测和转换
        if is_deepseek_r1_series_models(llm):
            for i in range(len(kwargs["chat_history"])):
                if isinstance(kwargs["chat_history"][i], SystemMessage):
                    msg = convert_message_to_dict(kwargs["chat_history"][i])
                    msg["role"] = "user"
                    kwargs["chat_history"][i] = convert_dict_to_message(msg)

        _logger.info(f"intent recognition results: {request_local.intent_recognition_results}")
        return llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs

    def context_compressor_pipeline(
        self, llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs
    ):
        # NOTE: 目前只对 StructuredChatCommonQAAgent 进行处理
        if isinstance(self, StructuredChatCommonQAAgent):
            if "chat_history" in kwargs and kwargs["chat_history"]:
                # 用来压缩知识库知识/工具调用结果所需提供的 chat history（倒数取最新的）
                provided_chat_history = deepcopy(kwargs["chat_history"])[
                    -kwargs.get("max_n_chat_history_for_compress", 5) :
                ]
            else:
                provided_chat_history = []

            self.ensure_agent_token_limit(
                llm,
                chat_prompt_template,
                candidate_tools,
                intermediate_steps,
                provided_chat_history,
                kwargs,
            )

            # 对于工具调用结果，直接再加个特殊判断
            # 且使用字符串长度而不使用 token 计数，减少计算 token 的耗时
            if isinstance(self, ToolCallingCommonQAAgent):
                agent_scratchpad = format_to_tool_messages(intermediate_steps)
            elif isinstance(self, StructuredChatCommonQAAgent):
                agent_scratchpad = enhanced_format_log_to_str(intermediate_steps)
            if len(agent_scratchpad) > kwargs.get("tool_output_compress_thrd", 5000):
                conditional_dispatch_custom_event(
                    "custom_event",
                    {"compress_log": "\n```text\n工具调用结果过长，尝试压缩工具调用结果以减少 token 使用。\n```\n"},
                    **kwargs,
                )
                self.__class__.intent_recognition_instance.llm_intermediate_step_compressor_parallel(
                    provided_chat_history,
                    kwargs["query"],
                    intermediate_steps,
                    llm,
                )

        return llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs

    def intent_recognition_with_context_compressor(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        kwargs: Any = None,
    ):
        (
            llm,
            chat_prompt_template,
            candidate_tools,
            intermediate_steps,
            callbacks,
            kwargs,
        ) = self.intent_recognition_pipeline(intermediate_steps, callbacks, kwargs)

        (
            llm,
            chat_prompt_template,
            candidate_tools,
            intermediate_steps,
            callbacks,
            kwargs,
        ) = self.context_compressor_pipeline(
            llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs
        )

        return llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs

    def custom_plan(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Union[AgentAction, AgentFinish, None]:
        """自定义返回逻辑
        如果需要返回AgentFinish，只需按照以下示例：
        ```
        return AgentFinish(
            return_values={"output": ""},
            log="",
        )
        ```
        """

        (
            llm,
            chat_prompt_template,
            candidate_tools,
            intermediate_steps,
            callbacks,
            kwargs,
        ) = self.intent_recognition_with_context_compressor(intermediate_steps, callbacks, kwargs)

        return AgentAction(
            tool="intent_recognition_tool",
            tool_input={
                "llm": llm,
                "prompt": chat_prompt_template,
                "tools": candidate_tools,
                "intermediate_steps": intermediate_steps,
                "callbacks": callbacks,
                "kwargs": kwargs,
            },
            log="调用意图识别模块",
        )

    def _handle_custom_plan(
        self,
        custom_ret: Union[AgentAction, AgentFinish],
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks,
        kwargs: Any,
    ) -> Tuple[Union[AgentAction, AgentFinish, None], List[Tuple[AgentAction, str]], Callbacks, Any]:
        if custom_ret:
            if isinstance(custom_ret, AgentAction) and custom_ret.tool == "intent_recognition_tool":
                self.llm, self.chat_prompt_template, self.tools, intermediate_steps, callbacks, kwargs = (
                    custom_ret.tool_input["llm"],
                    deepcopy(custom_ret.tool_input["prompt"]),
                    custom_ret.tool_input["tools"],
                    custom_ret.tool_input["intermediate_steps"],
                    custom_ret.tool_input["callbacks"],
                    custom_ret.tool_input["kwargs"],
                )
                agent_runnable = self.create_agent_func(self.llm, self.tools, self.chat_prompt_template)
                kwargs["runnable_has_been_modified"] = True
                self.runnable = agent_runnable
            else:
                kwargs["runnable_has_been_modified"] = True
                self.runnable = self.raw_runnable
                return custom_ret, intermediate_steps, callbacks, kwargs
        return None, intermediate_steps, callbacks, kwargs

    def plan(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Union[AgentAction, AgentFinish]:
        custom_ret = self.custom_plan(intermediate_steps, callbacks, **kwargs)
        result, intermediate_steps, callbacks, kwargs = self._handle_custom_plan(
            custom_ret, intermediate_steps, callbacks, kwargs
        )
        if result is not None:
            return result
        return super().plan(intermediate_steps, callbacks, **kwargs)

    async def aplan(
        self,
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Union[AgentAction, AgentFinish]:
        custom_ret = await sync_to_async(self.custom_plan)(intermediate_steps, callbacks, **kwargs)
        result, intermediate_steps, callbacks, kwargs = self._handle_custom_plan(
            custom_ret, intermediate_steps, callbacks, kwargs
        )
        if result is not None:
            return result
        return await super().aplan(intermediate_steps, callbacks, **kwargs)

    def format_and_check_token_length(
        self,
        llm,
        chat_prompt_template,
        candidate_tools,
        intermediate_steps,
        kwargs,
    ):
        inner_input = {}
        if "agent_scratchpad" in chat_prompt_template.input_variables:
            if isinstance(self, ToolCallingCommonQAAgent):
                inner_input["agent_scratchpad"] = format_to_tool_messages(intermediate_steps)
            elif isinstance(self, StructuredChatCommonQAAgent):
                inner_input["agent_scratchpad"] = enhanced_format_log_to_str(intermediate_steps)
        if "beijing_now" in chat_prompt_template.input_variables:
            inner_input["beijing_now"] = get_beijing_now()
        if "context" in chat_prompt_template.input_variables:
            inner_input["context"] = kwargs["context"]
        if "query" in chat_prompt_template.input_variables:
            inner_input["query"] = kwargs["query"]
        if "role_prompt" in chat_prompt_template.input_variables:
            inner_input["role_prompt"] = kwargs["role_prompt"]
        if "tool_names" in chat_prompt_template.input_variables:
            inner_input["tool_names"] = ", ".join([t.name for t in candidate_tools])
        if "tools" in chat_prompt_template.input_variables:
            inner_input["tools"] = render_text_description_and_args(list(candidate_tools))
        if "chat_history" in chat_prompt_template.partial_variables:
            inner_input["chat_history"] = kwargs["chat_history"]

        formated_prompts = chat_prompt_template._format_prompt_with_error_handling(inner_input)
        cur_token_len = llm.get_num_tokens_from_messages(formated_prompts.messages)
        return cur_token_len, formated_prompts

    def ensure_agent_token_limit(
        self,
        llm,
        chat_prompt_template,
        candidate_tools,
        intermediate_steps,
        provided_chat_history,
        kwargs,
    ):
        cur_token_len, formated_prompts = self.format_and_check_token_length(
            llm,
            chat_prompt_template,
            candidate_tools,
            intermediate_steps,
            kwargs,
        )
        first_entry = True
        has_executed_context_compressor = False
        has_executed_intermediate_step_compressor = False
        token_limit_margin = kwargs.get("token_limit_margin", 100)
        llm_token_limit = getattr(self, "llm_token_limit", 28000)
        while cur_token_len > llm_token_limit - token_limit_margin:
            # 优先级 1: 压缩召回的知识的内容
            if "context" in kwargs and kwargs["context"] and not has_executed_context_compressor:
                conditional_dispatch_custom_event(
                    "custom_event",
                    {"compress_log": "\n```text\nToken 超限，尝试压缩知识库知识内容以减少 token 使用。\n```\n"},
                    **kwargs,
                )
                kwargs["context"] = self.__class__.intent_recognition_instance.llm_context_compressor_parallel(
                    provided_chat_history,
                    kwargs["query"],
                    kwargs["context"],
                    llm,
                )
                has_executed_context_compressor = True
            # 优先级 2: 压缩 intermediate steps 的内容
            elif intermediate_steps and not has_executed_intermediate_step_compressor:
                if not isinstance(self, StructuredChatCommonQAAgent):
                    raise RuntimeError("当前仅支持对 StructuredChatCommonQAAgent 进行工具调用中间结果总结压缩！")
                conditional_dispatch_custom_event(
                    "custom_event",
                    {"compress_log": "\n```text\nToken 超限，尝试压缩工具调用结果以减少 token 使用。\n```\n"},
                    **kwargs,
                )
                self.__class__.intent_recognition_instance.llm_intermediate_step_compressor_parallel(
                    provided_chat_history,
                    kwargs["query"],
                    intermediate_steps,
                    llm,
                )
                has_executed_intermediate_step_compressor = True
            # 优先级 3: 依次抛除 chat history 内容
            elif "chat_history" in kwargs and kwargs["chat_history"]:
                if first_entry:
                    conditional_dispatch_custom_event(
                        "custom_event",
                        {"compress_log": "\n```text\nToken 超限，尝试抛除会话历史以减少 token 使用。\n```\n"},
                        **kwargs,
                    )
                kwargs["chat_history"].pop(0)
                first_entry = False
            else:
                err_msg = (
                    "已尝试按优先级压缩上下文，但还是超过 token 限制，无法回答问题，请尝试其他 LLM。"
                    f"（注：当前所需 token 数为：{cur_token_len}；支持的 LLM token 数为：{llm_token_limit(llm.model_name)}；"
                    f"设置的 token limit margin 为：{token_limit_margin}。）"
                )
                _logger.error(err_msg)
                _logger.error(f"=====> formated_prompts: \n{formated_prompts.messages}\n")
                raise RuntimeError(err_msg)

            cur_token_len, formated_prompts = self.format_and_check_token_length(
                llm,
                chat_prompt_template,
                candidate_tools,
                intermediate_steps,
                kwargs,
            )

    @classmethod
    def knowledge_resources_postproc(cls, kwargs, recog_results, knowledge_resource_type):
        # NOTE: 如果有 index_content 且是结构化数据则取 index_content，否则才取 page_content（兼容写法）。
        # 待知识库后台对非结构化数据的处理方式的 index_content 不是默认使用LLM总结后的内容之后，
        # 可将“且是结构化数据”的逻辑去除
        # NOTE: 目前暂不考虑检索返回模板对 page_content 的影响
        kwargs["context"] = [
            (
                doc["metadata"]["index_content"]
                if "index_content" in doc["metadata"] and is_structured_data(doc)
                else doc["page_content"]
            )
            for doc in recog_results[knowledge_resource_type]
        ]
        if reference_doc := deduplicate_knowledge_file_paths(recog_results[knowledge_resource_type]):
            conditional_dispatch_custom_event(
                "custom_event",
                {"reference_doc": reference_doc},
                **kwargs,
            )
            if hasattr(request_local, "current_user_store"):
                request_local.current_user_store["reference_doc"] = reference_doc

    @classmethod
    def intent_recognition(
        cls,
        llm: BaseChatModel,
        prefix: str,
        role_prompt: str,
        tools: List[BaseTool],
        intermediate_steps: List[Tuple[AgentAction, str]],
        callbacks: Callbacks = None,
        force_process_by_agent=False,
        config=None,
        **kwargs: Any,
    ) -> Tuple[BaseChatModel, ChatPromptTemplate, List[BaseTool], List[Tuple[AgentAction, str]], Callbacks, Any]:
        """
        :param prefix: aidev 默认为用户配的 system prompt。
        :param role_prompt: 用户在 aidev 页面上创建 agent 时填写的 prompt。
            旧主站逻辑会将其与 prefix 拼接后作为整体外层 agent 的 system prompt。
        :param force_process_by_agent: 是否强制进入 IntentStatus.PROCESS_BY_AGENT 的 status。用于 AIDEV 产品页面召回测试。
        """
        if config:
            _set_config_context(config)
        query = kwargs["input"]
        # NOTE: 加上意图识别流程后，需要把默认自带的 `knowledge_query` 去掉
        tools_for_intent_recog = deduplicate_tools([tool for tool in deepcopy(tools) if tool.name != "knowledge_query"])
        reject_threshold = tuple(map(float, settings.BKAIDEV_KNOWLEDGE_RESOURCE_REJECT_THRESHOLD.split(",")))
        recog_results = cls.intent_recognition_instance.exec_intent_recognition(
            query,
            llm,
            tools_for_intent_recog,
            callbacks,
            force_process_by_agent=force_process_by_agent,
            with_structured_data=kwargs.pop("with_structured_data", False),  # pop 防止跟后续的 **kwargs 重复。下同
            knowledge_bases=kwargs.pop("knowledge_bases", []),
            knowledge_items=kwargs.pop("knowledge_items", []),
            knowledge_resource_rough_recall_topk=kwargs.pop("topk", settings.BKAIDEV_TOP_K),
            knowledge_resource_reject_threshold=kwargs.pop(
                "knowledge_resource_reject_threshold",
                reject_threshold,
            ),
            knowledge_resource_fine_grained_score_type=kwargs.pop(
                "knowledge_resource_fine_grained_score_type",
                FineGrainedScoreType(settings.BKAIDEV_FINE_GRAINED_SCORE_TYPE),
            ),
            tool_resource_base_ids=None,  # 待工具类资源注册表支持后，改成从kwargs中取
            **kwargs,
        )
        if force_process_by_agent and recog_results["status"] != IntentStatus.PROCESS_BY_AGENT:
            raise RuntimeError(
                "force_process_by_agent 的情况下状态值必须是 IntentStatus.PROCESS_BY_AGENT"
                f"当前状态值为{recog_results['status']}"
            )

        # 不同 Agent 使用不同的 chat prompt template
        if issubclass(cls, ToolCallingCommonQAAgent):
            chat_prompt_template_variable_suffix = "_tool_calling"
        elif issubclass(cls, StructuredChatCommonQAAgent):
            chat_prompt_template_variable_suffix = "_structured_chat"
        if "hunyuan" in llm.model_name and chat_prompt_template_variable_suffix == "_structured_chat":
            raise RuntimeError("混元的prompt除system之外必须是一问一答形式，请检查 chat prompt template")

        # 根据不同的 IntentStatus 分别进行处理
        if recog_results["status"] == IntentStatus.QA_WITH_RETRIEVED_KNOWLEDGE_RESOURCES:
            candidate_tools = []
            independent_query = query
            chat_prompt_template = cls.qa_prompt_templates.get(
                f"private_qa_prompt{chat_prompt_template_variable_suffix}"
            )
            kwargs["context"] = recog_results["retrieved_knowledge_resources"]
        elif recog_results["status"] == IntentStatus.AGENT_FINISH_WITH_RESPONSE:
            raise NotImplementedError(
                "需要将recog_results['response']作为AgentFinish的return_values的output返回后"
                "在外层调用时直接返回AgentFinish"
            )
        elif recog_results["status"] == IntentStatus.DIRECTLY_RESPOND_BY_AGENT:
            candidate_tools = []
            independent_query = query
            chat_prompt_template = cls.qa_prompt_templates.get(
                f"general_qa_prompt{chat_prompt_template_variable_suffix}"
            )
        elif recog_results["status"] == IntentStatus.PROCESS_BY_AGENT:
            candidate_tools = recog_results["candidate_tools"]
            independent_query = recog_results["independent_query"]
            if recog_results["decision"] == Decision.GENERAL_QA:
                chat_prompt_template = cls.qa_prompt_templates.get(
                    f"general_qa_prompt{chat_prompt_template_variable_suffix}"
                )
            elif recog_results["decision"] == Decision.PRIVATE_QA:
                chat_prompt_template = cls.qa_prompt_templates.get(
                    f"private_qa_prompt{chat_prompt_template_variable_suffix}"
                )
                cls.knowledge_resources_postproc(
                    kwargs, recog_results, knowledge_resource_type="knowledge_resources_highly_relevant"
                )
            elif recog_results["decision"] == Decision.QUERY_CLARIFICATION:
                if query_clarification_enabled(llm, kwargs):
                    chat_prompt_template = cls.qa_prompt_templates.get(
                        f"clarifying_qa_prompt{chat_prompt_template_variable_suffix}"
                    )
                else:
                    chat_prompt_template = cls.qa_prompt_templates.get(
                        f"private_qa_prompt{chat_prompt_template_variable_suffix}"
                    )
                cls.knowledge_resources_postproc(
                    kwargs, recog_results, knowledge_resource_type="knowledge_resources_moderately_relevant"
                )

        # NOTE: 如果是多模态场景，可能会有一个特殊的 `add_image_to_chat_context` 工具，需要带上
        # 但是仅针对支持 multimodal 才添加
        # NOTE: 如果是支持 Function Calling 的模型， 使用 ToolCallCommonAgent 会要求 tools 不能为空，否则会报错！
        if support_multimodal(llm):
            candidate_tools.extend([tool for tool in tools if tool.name == "add_image_to_chat_context"])
            candidate_tools = deduplicate_tools(candidate_tools)
        else:
            candidate_tools = deduplicate_tools(
                [tool for tool in deepcopy(candidate_tools) if tool.name != "add_image_to_chat_context"]
            )

        # 补充/修改 kwargs 的值
        if kwargs.get("use_independent_query_in_qa", False):
            kwargs["query"] = independent_query
        else:
            # 默认在最终提问的时候使用原始的用户 query
            # independent query 只用于知识库召回
            kwargs["query"] = kwargs["input"]
        kwargs["role_prompt"] = role_prompt
        kwargs["recog_results"] = recog_results
        # 补充/修改 kwargs 的值：给 AIDEV 产品检索测试模块使用
        if force_process_by_agent:
            kwargs["decision"] = recog_results["decision"]
            kwargs["retrieved_docs"] = filter_and_select_topk(
                recog_results["knowledge_resources_emb_recalled"],
                kwargs.get("score_threshold"),
                kwargs.get("topk", 20),
            )
            kwargs["beijing_now"] = get_beijing_now()

        return llm, chat_prompt_template, candidate_tools, intermediate_steps, callbacks, kwargs


class CommonQAStreamingMixIn:
    LOADING_AGENT_MESSAGE: str = "正在思考..."
    think_symbols: List[str] = [
        "<think>\n",
        "\n</think>\n",  # 不要用"\n</think>\n\n"，留个"\n"以让后续```前面带"\n"，方便用markdown语法渲染
    ]
    final_answer_prefixes: List[str] = deepcopy(FINAL_ANSWER_PREFIXES)
    final_answer_suffixes: List[str] = deepcopy(FINAL_ANSWER_SUFFIXES)
    # NOTE: 人工定义结束标志，用于去除 final_answer_suffix 时往后判断是否已经到达末尾
    # 这是因为 Final Answer 内部本身可能刚好有 final_answer_suffix 这种模式，直接过滤有误删风险。
    # 因此使用缓冲队列，往后读到结束标志，再进行以下剔除操作。
    end_content = "<｜end▁of▁sentence｜>"

    def common_filter(self, cache, filter_symbols, event_type):
        hit = False
        recall_event = None
        combined_content = "".join(
            [item["content"] for item in cache if "content" in item and item.get("event") == event_type]
        )
        for symbol in filter_symbols:
            if symbol in combined_content:
                # 如果是 final_answer_prefix 这种特殊情况，外层逻辑会补充一个 recall_ret 回来
                # 因此这里可以放心地从 think 中去除末尾的那个需要归属 text 的块
                if symbol in self.final_answer_prefixes and not combined_content.endswith(symbol):
                    start_index = combined_content.find(symbol)
                    combined_content = combined_content[:start_index]
                else:
                    combined_content = combined_content.replace(symbol, "")
                hit = True
                break
        if hit:
            if combined_content:
                recall_event = {"event": event_type, "content": combined_content, "cover": False}
            # think 类型有个特殊处理：需要保证在最后一个 think event 上带上 elapsed_time
            if event_type == EventType.THINK.value:
                for item in cache:
                    if elapsed_time := item.get("elapsed_time"):
                        if recall_event:
                            recall_event["elapsed_time"] = elapsed_time
                        else:
                            recall_event = {
                                "event": event_type,
                                "content": "",
                                "cover": False,
                                "elapsed_time": elapsed_time,
                            }
                        break
        return hit, recall_event

    def cache_filter(self, cache, final_answer_prefix_to_filter=None, final_answer_suffix_to_filter=None):
        """
        注意！对于类似以下格式的内容，需要把"好的"留下！
        ```
        cache = deque(
            [
                {"event": "think", "content": "<th", "cover": False},
                {"event": "think", "content": "in", "cover": False},
                {"event": "think", "content": "k>", "cover": False},
                {"event": "think", "content": "\n好的", "cover": False},
            ]
        )
        ```
        """
        # 针对 think event 的过滤
        think_event_filter_symbols = deepcopy(self.think_symbols)
        if final_answer_prefix_to_filter:
            think_event_filter_symbols.append(final_answer_prefix_to_filter)
        hit_think, recall_event_think = self.common_filter(cache, think_event_filter_symbols, EventType.THINK.value)

        # 针对 final answer JSON BLOB 后缀的过滤
        if final_answer_suffix_to_filter:
            hit_suffix, recall_event_suffix = self.common_filter(
                cache, [final_answer_suffix_to_filter], EventType.TEXT.value
            )
        else:
            hit_suffix = False
            recall_event_suffix = None

        if hit_think or hit_suffix:
            init_events = list(deepcopy(cache))
            if hit_think:
                # 去除 think 的 event，因为已经合并和过滤了
                # 需保证 think 在 text 前面
                remain_events = []
                have_appended_recall_event_think = False
                for event in init_events:
                    if event.get("event") == EventType.THINK.value:
                        if recall_event_think and not have_appended_recall_event_think:
                            remain_events.append(recall_event_think)
                            have_appended_recall_event_think = True
                    else:
                        remain_events.append(event)
            else:
                remain_events = init_events

            if hit_suffix:
                # 去除 text 的 event，因为已经合并和过滤了
                # 需保证 think 在 text 前面
                # 因为认为 cache 中原始的 think 一定在 text 前面，所以这样处理即可：
                remain_events = [event for event in remain_events if event.get("event") != EventType.TEXT.value]
                if recall_event_suffix:
                    remain_events.append(recall_event_suffix)

            cache = deque(remain_events)

        return cache
    
    def check_and_append(self, cache, ret):
        """在刚从 think 切换到 text 逻辑之前需要进行的特殊处理"""
        # 前端渲染要求在 think 和 text 之间必须保证有 "\n\n" 的 text 内容
        if (
            cache
            and (cache[-1].get("event") == EventType.THINK.value)
            and (ret.get("event") == EventType.TEXT.value)
            and (not ret.get("content").startswith("\n"))
        ):
            if ret.get("content"):
                ret["content"] = "\n\n" + ret["content"]
            else:
                ret["content"] = "\n\n"
        cache.append(ret)

    def stream_standard_event(self, agent_e, cfg, input_, skip_thought=True, timeout: Optional[int] = None):
        """
        如果 is_deepseek_r1_series_models(self.llm)，则需要：
           统一：去除 think 标识位

           a) 如果 isinstance(self, StructuredChatCommonQAAgent)，则需要：
              将 think 过程和中间的 agent action 过程都作为 think event 发送
              用自定义的匹配流程来判断
           b) 如果 isinstance(self, ToolCallingCommonQAAgent)，则需要：
              将 think 过程作为 think event 发送
              用 reasoning content 来判断
        """
        run_info = defaultdict(dict)
        first_chunk = True
        final_result = ""
        non_think_content = ""
        last_ret_is_empty = False
        front_end_display = True
        if is_deepseek_r1_series_models(self.llm) or "deepseek-v3" in self.llm.model_name:
            # 用于去除 think 标识位
            max_cache_length = 50
            cache = deque(maxlen=max_cache_length)
            agent_think_start_time = time.time()
            # 用于判断 done 之前的最后一个 event 类型
            last_event_type = None
            final_answer_prefix_to_filter = None
            final_answer_suffix_to_filter = None
            if isinstance(self, StructuredChatCommonQAAgent):
                # 在 StructuredChatCommonQAAgent 中用于合并 agent action 中间过程
                # 在出现 Final Answer 模式之前的所有过程都视为 agent 的 think 过程，因此初始化为 EventType.THINK.value
                cur_event_type = EventType.THINK.value
                final_answer_occurred = False
                first_time_final_answer = True
            elif isinstance(self, ToolCallingCommonQAAgent):
                has_sent_elapsed_time = False
        try:
            for item in agent_e.stream_events(input_, config=cfg, version="v2", timeout=timeout):
                ret = {}
                recall_ret = {}
                if item == Empty:
                    if last_ret_is_empty or first_chunk:
                        ret = {
                            "event": EventType.TEXT.value,
                            "content": self.LOADING_AGENT_MESSAGE,
                            "cover": last_ret_is_empty,
                        }
                else:
                    cover = bool(last_ret_is_empty)
                    if item["event"] == "on_chat_model_stream" and front_end_display:
                        if item["data"]["chunk"].tool_calls:
                            run_info[item["run_id"]]["tool_call"] = True
                        is_tool_call = run_info[item["run_id"]].get("tool_call")
                        if skip_thought and is_tool_call:
                            continue
                        if not item["data"]["chunk"].content and not item["data"]["chunk"].additional_kwargs.get(
                            "reasoning_content", None
                        ):
                            continue
                        if is_deepseek_r1_series_models(self.llm) or "deepseek-v3" in self.llm.model_name:
                            if isinstance(self, StructuredChatCommonQAAgent):
                                # 如果是 StructuredChatCommonQAAgent，则会将所有中间 action 步骤也归为 think
                                # 判断最终答案的逻辑在后面，所以这里先统一成 text
                                if reasoning_content := item["data"]["chunk"].additional_kwargs.get(
                                    "reasoning_content", None
                                ):
                                    content = reasoning_content
                                else:
                                    content = item["data"]["chunk"].content
                                    non_think_content += content
                                ret = {
                                    "event": EventType.TEXT.value,
                                    "content": content,
                                    "cover": cover,
                                }
                            elif isinstance(self, ToolCallingCommonQAAgent):
                                # 如果是 CommonQAAgent，则判断最终答案的逻辑在这里
                                if reasoning_content := item["data"]["chunk"].additional_kwargs.get(
                                    "reasoning_content", None
                                ):
                                    ret = {
                                        "event": EventType.THINK.value,
                                        "content": reasoning_content,
                                        "cover": cover,
                                    }
                                else:
                                    # 如果首次收到 text 内容，说明是从 think 逻辑切过来的，需要先补发一条带 elapsed_time
                                    # 的 think event 以供识别
                                    if not has_sent_elapsed_time:
                                        has_sent_elapsed_time = True
                                        ret = {
                                            "event": EventType.THINK.value,
                                            "content": "\n",
                                            "cover": False,
                                            "elapsed_time": (time.time() - agent_think_start_time) * 1000,
                                        }
                                        self.check_and_append(cache, ret)
                                    ret = {
                                        "event": EventType.TEXT.value,
                                        "content": item["data"]["chunk"].content,
                                        "cover": cover,
                                    }
                                    non_think_content += item["data"]["chunk"].content
                        else:
                            ret = {
                                "event": EventType.TEXT.value,
                                "content": item["data"]["chunk"].content,
                                "cover": cover,
                            }
                            non_think_content += item["data"]["chunk"].content
                        final_result += ret["content"]
                    elif item["event"] == "on_custom_event":
                        if "front_end_display" in item["data"]:
                            # 如果接收到 front_end_display 标识位的信息，则更新 front_end_display
                            front_end_display = item["data"]["front_end_display"]
                        elif "custom_return_chunk" in item["data"] and front_end_display:
                            ret = {
                                "event": EventType.TEXT.value,
                                "content": item["data"]["custom_return_chunk"],
                                "cover": cover,
                            }
                            final_result += ret["content"]
                        elif "reference_doc" in item["data"] and front_end_display:
                            ret = {
                                "event": EventType.REFERENCE_DOC.value,
                                "documents": item["data"]["reference_doc"],
                                "cover": cover,
                            }
                        elif "compress_log" in item["data"] and front_end_display:
                            ret = {
                                "event": EventType.THINK.value,
                                "content": item["data"]["compress_log"],
                                "cover": cover,
                            }
                        elif "custom_agent_finish" in item["data"] and front_end_display:
                            # 专为用户需自定义 agent finish 逻辑且需要使用 stream 调用的情况设计
                            ret = {
                                "event": EventType.TEXT.value,
                                "content": item["data"]["custom_agent_finish"],
                                "cover": cover,
                            }
                            final_result += item["data"]["custom_agent_finish"]
                    elif isinstance(self, StructuredChatCommonQAAgent) and item["event"] == "on_tool_end":
                        # TODO: 可能需要考虑异步是否会导致event的乱序问题
                        # 打印工具输出
                        tool_output_content = str(item["data"]["output"])
                        # 报错信息封装
                        if " is not a valid tool, try one of " in tool_output_content:
                            err_tool = tool_output_content.split(" is not a valid tool, try one of ")[0]
                            tool_output_content = f"LLM 选择的工具“{err_tool}”超出了给定工具的范围，本次工具调用失败。"
                        elif tool_output_content == ACTION_INPUT_ERR_MSG:
                            tool_output_content = "LLM 生成的工具调用参数不正确，本次工具调用失败。"
                        elif tool_output_content == OUTPUT_PARSER_ERR_MSG:
                            tool_output_content = OUTPUT_PARSER_ERR_MSG

                        max_tool_output_len = 500
                        if len(tool_output_content) > max_tool_output_len:
                            tool_output_content = tool_output_content[:max_tool_output_len] + "（内容过长，已截断）"
                        # NOTE: 重要操作！
                        # 由于 LLM 输出结果不可控，为了防止 stream 过程中输出的 JSON BLOB 中有开始的 ``` 而没有结束的 ```
                        # 这里在返回工具调用结果之前，前判断当前 final_result 中 ``` 已经出现的次数
                        # 如果是奇数次，则手工拼接一个 ``` 防止前端渲染的时候乱了
                        log_prefix = "\n```\n" if final_result.count("```") % 2 == 1 else ""
                        ret = {
                            "event": EventType.TEXT.value,
                            "content": (
                                f"{log_prefix}\n\n以下是该 Agent Action 的结果："
                                f"\n```text\n{tool_output_content}\n```\n\n"
                            ),
                            "cover": cover,
                        }
                        final_result += ret["content"]
                    if isinstance(self, StructuredChatCommonQAAgent):
                        if "content" in ret:
                            ret["event"] = cur_event_type
                        # 一旦出现 Final Answer 模式，之后的所有过程都视为 agent 的正式回答过程
                        # NOTE: 需要在 non_think_content 中匹配到的 Final Answer 才能触发结束，think 过程中匹配到的不算
                        if not final_answer_occurred:
                            for final_answer_prefix, final_answer_suffix in zip(
                                self.final_answer_prefixes, self.final_answer_suffixes
                            ):
                                if final_answer_prefix in non_think_content:
                                    final_answer_occurred = True
                                    final_answer_prefix_to_filter = final_answer_prefix
                                    # 注：后续放到 cache 前的 ret 内容从出现 final answer 的下一次开始进行了针对 \n 的转义操作
                                    # 为了不影响命中，此处也同步进行相同的转义操作
                                    final_answer_suffix_to_filter = final_answer_suffix.replace("\\n", "\n")
                                    # 需要加上特殊的 end_content 才能作为最终的 final_answer_suffix
                                    final_answer_suffix_to_filter += deepcopy(self.end_content)
                                    if not final_result.endswith(final_answer_prefix):
                                        # 这种情况下说明最终答案有一小块跟在了 final_answer_prefix 最后一个块的后面
                                        # 需要将这块内容补回来，并将 think 末尾的那段内容截掉
                                        # NOTE: 使用 rfind 寻找最后匹配的那个final answer，确保匹配到的是
                                        # non_think_content 中的 final answer 块
                                        start_index = final_result.rfind(final_answer_prefix)
                                        if start_index == -1:
                                            raise RuntimeError(
                                                f"结果子串提取有误。\nfinal_result: {final_result}\n"
                                                f"final_answer_prefix: {final_answer_prefix}\n"
                                            )
                                        end_index = start_index + len(final_answer_prefix)
                                        recall_ret_prefix_content = final_result[end_index:]
                                        try:
                                            ret["content"] = ret["content"][: -len(recall_ret_prefix_content)]
                                        except Exception:
                                            raise RuntimeError(
                                                f"子串去除有误。\nret: {ret}\n"
                                                f"recall_ret_prefix_content: {recall_ret_prefix_content}\n"
                                            )
                                        # 处理 json 格式内的 final answer 的内容（包含被转义的情况）以供 markdown 渲染。
                                        # TODO: 同步更新 final_result
                                        # NOTE: 需要在上述 ret["content"][: -len(recall_ret_prefix_content)] 之后
                                        # 执行这个动作！否则会把 ret["content"] 多去了一个字符
                                        recall_ret_prefix_content = recall_ret_prefix_content.replace("\\n", "\n")
                                        recall_ret = {
                                            "event": EventType.TEXT.value,
                                            "content": recall_ret_prefix_content,
                                            "cover": cover,
                                        }
                                    cur_event_type = EventType.TEXT.value
                                    ret["elapsed_time"] = (time.time() - agent_think_start_time) * 1000
                                    break
                        if final_answer_occurred and not first_time_final_answer:  # noqa: SIM102
                            # NOTE: final_answer_suffix 有可能不在同一个 ret 的 content 中，
                            # 所以 final_answer_suffix 不能在此处原地剔除，
                            # 需要在 cache 中合并判断和剔除。例子如下所示：
                            # =====> {'event': 'text', 'content': '）。', 'cover': False}
                            # =====> {'event': 'text', 'content': '"\n', 'cover': False}
                            # =====> {'event': 'text', 'content': '}\n', 'cover': False}
                            # =====> {'event': 'text', 'content': '```', 'cover': False}
                            # NOTE: 如果 first_time_final_answer 为 True，则是刚从 think 变成 text 的时候，
                            # 当前的 ret 就还不属于 final answer 类型，因此不能进入本分支

                            # 以下内容用于处理 json 格式内的 final answer 的内容（包含被转义的情况）以供 markdown 渲染。
                            # TODO: 同步更新 final_result
                            # 目前仅支持处理换行符：\\n --> \n
                            if "content" in ret:
                                ret["content"] = ret["content"].replace("\\n", "\n")
                                if (
                                    cache
                                    and cache[-1].get("content", "").endswith("\\")
                                    and ret["content"].startswith("n")
                                ):
                                    # 处理这样的case：
                                    # data: {"event": "text", "content": "如下", "cover": false}
                                    # data: {"event": "text", "content": "：", "cover": false}
                                    # data: {"event": "text", "content": "\\", "cover": false}
                                    # data: {"event": "text", "content": "n1", "cover": false}
                                    # data: {"event": "text", "content": ".", "cover": false}
                                    # data: {"event": "text", "content": " 下", "cover": false}
                                    # data: {"event": "text", "content": "载", "cover": false}
                                    cache[-1]["content"] = cache[-1]["content"][:-1]
                                    ret["content"] = "\n" + ret["content"][1:]
                        # 更新标识变量
                        if final_answer_occurred:
                            first_time_final_answer = False
                if ret:
                    first_chunk = False
                    last_ret_is_empty = ret.get("content", "") == self.LOADING_AGENT_MESSAGE
                    if is_deepseek_r1_series_models(self.llm) or "deepseek-v3" in self.llm.model_name:
                        if ret.get("content", "") == self.LOADING_AGENT_MESSAGE:
                            last_event_type = ret["event"]
                            yield f"data: {json.dumps(ret)}\n\n"
                        else:
                            # NOTE: 只有非 self.LOADING_AGENT_MESSAGE 的 event 可以放到 cache 中
                            self.check_and_append(cache, ret)
                            if recall_ret:
                                # 如果 cache 非空，先 pop 最开始的元素，再将补充的 recall_ret 给添加进来
                                if cache:
                                    ret = cache.popleft()
                                    last_event_type = ret["event"]
                                    yield f"data: {json.dumps(ret)}\n\n"
                                self.check_and_append(cache, ret)
                            cache = self.cache_filter(
                                cache, final_answer_prefix_to_filter, final_answer_suffix_to_filter
                            )
                            if len(cache) == max_cache_length:
                                ret = cache.popleft()
                                last_event_type = ret["event"]                   
                                if not (last_event_type == EventType.THINK.value and ret["content"].strip() == ""):
                                    yield f"data: {json.dumps(ret)}\n\n"
                    else:
                        yield f"data: {json.dumps(ret)}\n\n"
            if is_deepseek_r1_series_models(self.llm) or "deepseek-v3" in self.llm.model_name:
                if isinstance(self, StructuredChatCommonQAAgent):
                    # 以下逻辑用于利用 self.end_content 标志跟 final_answer_suffix_to_filter 拼接后进行尾部去除
                    if len(cache) == max_cache_length:
                        ret = cache.popleft()
                        last_event_type = ret["event"]
                        yield f"data: {json.dumps(ret)}\n\n"
                    end_ret = {
                        "event": EventType.TEXT.value,
                        "content": deepcopy(self.end_content),
                        "cover": False,
                    }
                    self.check_and_append(cache, ret)
                    len_before_filtering = len(cache)
                    cache = self.cache_filter(cache, final_answer_prefix_to_filter, final_answer_suffix_to_filter)
                    if len(cache) == len_before_filtering:
                        # 如果没 filter 到，则还是将 end_ret 剔除
                        cache.pop()

                while cache:
                    ret = cache.popleft()
                    last_event_type = ret["event"]
                    if not (last_event_type == EventType.THINK.value and ret["content"].strip() == ""):
                        yield f"data: {json.dumps(ret)}\n\n"
                for think_symbol in self.think_symbols:
                    final_result = final_result.replace(think_symbol, "")
                # 如果 done 之前的最后一个 event 是 think 类型，则说明从 think 内容中解析结论失败，需额外发送一条 text event，
                # 防止报错：
                # {\"result\":false,\"data\":null,\"code\":\"1500400\",\"message\":\"content: 该字段不能为空。\"}" }
                if last_event_type == EventType.THINK.value:
                    # 先发一个确保带 elapsed_time 的 think ret
                    ret = {
                        "event": EventType.THINK.value,
                        "content": "\n",
                        "cover": False,
                        "elapsed_time": (time.time() - agent_think_start_time) * 1000,
                    }
                    yield f"data: {json.dumps(ret)}\n\n"
                    # 再发一个确保为 text 的 ret
                    _logger.warning(
                        "Fail to derive the final answer from the thinking process. "
                        f"The final result is: \n{final_result}\n"
                    )
                    ret = {
                        "event": EventType.TEXT.value,
                        "content": "抱歉，由于LLM指令遵从效果欠佳，尝试从思考内容中解析最终结论失败，请从思考内容中获取结论。",
                        "cover": cover,
                    }
                    yield f"data: {json.dumps(ret)}\n\n"
            # cover 为 True 时，final_result 为 stream 结束后需要最终显示的结果，可根据需要重新拼接
            # cover 为 False 时不进行覆盖
            cover = False

            ret = {
                "event": EventType.DONE.value,
                "content": final_result,
                "cover": cover,
            }
            yield f"data: {json.dumps(ret)}\n\n"
        except Exception as exception:
            ret = {
                "event": "error",
                "code": exception.code if hasattr(exception, "code") else 400,
                "message": exception.response_data() if hasattr(exception, "response_data") else str(exception),
            }
            _logger.exception(exception)
            yield f"data: {json.dumps(ret)}\n\n"
        finally:
            yield "data: [DONE]\n\n"


class ToolCallingCommonQAAgent(IntentRecognitionMixin, CommonQAStreamingMixIn, MultiToolCallCommonAgent):
    """适用于原生支持Function Calling的模型，如 hunyuan-turbo 模型"""


class StructuredChatCommonQAAgent(IntentRecognitionMixin, CommonQAStreamingMixIn, StructuredChatCommonAgent):
    """适用于没有原生支持Function Calling的模型，如DeepSeek R1 系列模型"""


class CommonQAAgent(ToolCallingCommonQAAgent):
    """
    普通用户直接使用 CommonQAAgent 即可，会进行 agent 自适应路由
    高级用户需根据使用情况继承不同的 agent，并在 CommonQAAgent 中注册使用
    NOTE: 这里先继承自 ToolCallingCommonQAAgent，因为 aidev.resource.chat_completion.logic.ChatCompletionApp.get_window
    中需要使用到 ensure_memory_window。待开发侧确认各类需要使用 CommonQAAgent 成员函数/属性的场景。
    """

    agent_classes: ClassVar[Dict] = {
        "tool_calling_common_qa_agent": ToolCallingCommonQAAgent,
        "structured_chat_common_qa_agent": StructuredChatCommonQAAgent,
    }

    @classmethod
    def register_agent_class(cls, key, agent_class):
        cls.agent_classes[key] = agent_class

    @classmethod
    def get_agent_executor(cls, *args, **kwargs):
        llm = kwargs["llm"] if "llm" in kwargs else args[0]
        extra_tools = kwargs.get("extra_tools", [])
        key = (
            "structured_chat_common_qa_agent"
            if (is_deepseek_r1_series_models(llm) or "deepseek-v3" in llm.model_name) and extra_tools
            else "tool_calling_common_qa_agent"
        )
        agent_class = cls.agent_classes.get(key, cls.agent_classes["tool_calling_common_qa_agent"])
        return agent_class.get_agent_executor(*args, **kwargs)
