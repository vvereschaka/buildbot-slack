# slack.py
#
# -*- python -*-
# ex: set filetype=python:

# Based on:
# rickmak/buildbot-slack
# https://github.com/rickmak/buildbot-slack
#
# Depends: txrequests or treq (HTTPClientService)
#   txrequests is 2.8x slower than treq due to the use of threads.
#       pip install txrequests
#           or
#       pip install treq
#
# Example (Buildbot 3.x):
#    builders = {
#        "builder-name-1" : ':hearts:' ,
#        "builder-name-2" : ':diamonds:'
#    }
#
#    import slack
#    master_config['services'].append(
#        slack.SlackNotifier(
#            webhookurl          = 'https://hooks.slack.com/services/<hook_configuration>',
#            debug               = False,
#            verbose             = True,
#            generators          = [
#                reporters.BuildStatusGenerator(
#                    mode = 'all',
#                    builders = list(builders.keys()),
#                    message_formatter = slack.SlackMessageFormatter(builder_emojiis=builders)),
#                reporters.WorkerMissingGenerator(
#                    workers = 'all',
#                    message_formatter = reporters.MessageFormatterMissingWorker())
#                ]
#        )

# Helpful link:
#  Web colors: https://en.wikipedia.org/wiki/Web_colors
#  Sending your first Slack message using Webhook: https://api.slack.com/tutorials/slack-apps-hello-world
#  Sending messages using Incoming Webhooks: https://api.slack.com/messaging/webhooks
#  Slack Block Kit Builder: https://api.slack.com/tools/block-kit-builder
#  Formatting text for app surfaces: https://api.slack.com/reference/surfaces/formatting
#  Reference: Composition objects: https://api.slack.com/reference/block-kit/composition-objects
#  Creating rich message layouts: https://api.slack.com/messaging/composing/layouts

# Slack API Python modules
#  https://github.com/slackapi/python-slackclient

# pylint: disable=missing-module-docstring,missing-function-docstring,missing-class-docstring
# pylint: disable=too-many-lines,too-many-arguments,line-too-long,invalid-name

from __future__ import absolute_import, print_function

import json

from six import string_types
#NOTE:VV: to check BB version within the message formater.
from pkg_resources import parse_version

from twisted.internet import defer
from twisted.python import log as logging

from buildbot import config
from buildbot import __version__ as buildbot_version
from buildbot.reporters import message
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.build import BuildStatusGenerator
from buildbot.reporters.generators.worker import WorkerMissingGenerator
from buildbot.reporters.message import MessageFormatterMissingWorker

from buildbot.util import httpclientservice

from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import FAILURE
from buildbot.process.results import RETRY
from buildbot.process.results import SKIPPED
from buildbot.process.results import SUCCESS
from buildbot.process.results import WARNINGS


if True:  # for debugging
    debuglog = logging.msg
else:
    debuglog = lambda m: None

def get_artifact_color_code():
    return "#CD853F"

def get_result_color_code(results):
    color = "#B22222"       # FireBrick/Red (FAILURE)
    if results == SUCCESS:
        color = "#008000"   # Green (SUCCESS)
    elif results == WARNINGS:
        color = "#FF8C00"   # DarkOrange (WARNINGS)
    elif results == EXCEPTION:
        color = "#FF00FF"   # Magenta (EXCEPTION)
    elif results == SKIPPED:
        color = "#00BFFF"   # DeepSkyBlue (SKIPPED)
    elif results == CANCELLED:
        color = "#F1948A"
    return color

def get_sourcestamp_message(source_stamps):
    text = []

    for ss in source_stamps:
        source = ""

        if ss['branch']:
            source += f"[branch {ss['branch']}] "

        if ss['revision']:
            source += str(ss['revision'])
        else:
            source += "HEAD"

        if ss['patch'] is not None:
            source += " (plus patch)"

        if ss['codebase']:
            source = f"*{ss['codebase']}*: {source}"

        text.append(source)

    return ", ".join(text)

def get_intro_message(mode, results, previous_results):
    if results == FAILURE:
        if "change" in mode and previous_results != results or \
                "problem" in mode and previous_results != FAILURE:
            text = "The Buildbot has detected *a new failure*"
        else:
            text = "The Buildbot has detected *a failed build*"
    elif results == WARNINGS:
        text = "The Buildbot has detected *a problem* in the build"
    elif results == SUCCESS:
        if "change" in mode and previous_results != results:
            text = "The Buildbot has detected *a restored build*"
        else:
            text = "The Buildbot has detected *a passing build*"
    elif results == EXCEPTION:
        text = "The Buildbot has detected *a build exception*"
    elif results == CANCELLED:
        text = "The Buildbot has detected a *cancelled build*"
    elif results == RETRY:
        text = "The Buildbot has detected a *retrying build*"
    else:
        text = "Intermediate build status"

    return text

def get_slack_attch_field(title, value, short=False):
    return {
        'title'     : title,
        'value'     : value,
        'short'     : short
    }

def get_slack_attch_action(name, text, value, action_type="button", style=None, confirm=None):
    action = {
        'name'      : name,
        'text'      : text,
        'value'     : value
    }

    if action_type is not None:
        action['type'] = action_type
    if style is not None:
        action['style'] = style
    if confirm is not None:
        action['confirm'] = confirm

    return action

_default_attch_args = {
    "color"             : "SlateGray",
    "attachment_type"   : "default"
}

#NOTE: the attachments are deprecatated in the latest Slack API version, but still supported.
# Redesign them to use the "blocks".
def get_slack_attch(fallback, title, **kwargs):
    """
    Build the slack message attachement.

    * fallback - Required plain-text summary of the attachment.

    The following fields (as arguments) are available:
        * title_link
        * pretext           - Optional text that appears above the attachment block.
        * text              - Optional text that appears within the attachment.
        * color             - "SlateGray" by default (more https://en.wikipedia.org/wiki/Web_colors)
        * author_name
        * author_link       - URL
        * author_icon       - URL
        * fields            - (array)
        * image_url         - URL
        * thumb_url         - URL
        * footer
        * footer_icon       - URL
        * ts                - timestamp (long integer value)
        Actions (buttons) related fields
        * callback_id
        * attachment_type   - "default"
        * actions           - (array)
    """

    attch = {
        'fallback'  : fallback,
        'title'     : title
    }

    # Copy arguments into the result attachement.
    for ar in kwargs:
        attch[ar] = kwargs.get(ar)

    # Add default field values.
    for ar in _default_attch_args.keys():
        if ar not in kwargs:
            attch[ar] = _default_attch_args[ar]

    return attch

def formatMentionUserId(uid):
    return f"<@{uid}>"

def formatMentionGroupId(gid):
    return f"<!subteam^{gid}>"

##
## Buildbot 3.x service.
##

class SlackMessageFormatter(message.MessageFormatterBase):
    name = "SlackMessageFormatter"

    def __init__(self, compact_message=False, builder_emojiis=None,
                 mentionUsers=None, mentionGroups=None, blamelistMap=None,
                 **kwargs):
        super().__init__(**kwargs)

        self.compact_message = compact_message
        self.builder_emojiis = builder_emojiis

        if self.builder_emojiis is not None and not isinstance(self.builder_emojiis, dict):
            config.error("'builder_emojiis' must be a dictionary or None")

        if mentionUsers is None:
            mentionUsers = []
        if not isinstance(mentionUsers, list):
            config.error("'mentionUsers' must be a list")

        self.mentionUsers = []
        # Prepare a list of formatted user's ids to mention.
        for r in mentionUsers:
            self.mentionUsers.append(formatMentionUserId(r))

        if mentionGroups is None:
            mentionGroups = []
        if not isinstance(mentionGroups, list):
            config.error("'mentionGroups' must be a list")

        self.mentionGroups = []
        # Prepare a list of formatted group's ids to mention.
        for r in mentionGroups:
            self.mentionGroups.append(formatMentionGroupId(r))

        if blamelistMap:
            if not isinstance(blamelistMap, dict):
                config.error("'blamelistMap' must be a dictionary")

        self.blamelistMap = blamelistMap

    def getEmojiiForBuilder(self, builderName):
        if self.builder_emojiis:
            return self.builder_emojiis.get(builderName, ":gear:")
        return ":gear:"

    def mapSlackUsersToBlamelist(self, blamelist):
        nbl = []

        for bl in blamelist:
            if bl in self.blamelistMap:
                slu = formatMentionUserId(self.blamelistMap[bl])
                nbl.append(f"{bl} ({slu})")
            else:
                nbl.append(bl)
        return nbl

    @defer.inlineCallbacks
    def format_message_for_build(self, master, build, is_buildset=False, users=None, mode=None, **kwargs):
        #NOTE:VV: create_context_for_build has a different set of the arguments between BB 3.4 and 3.5.
        # bb3.4: create_context_for_build(mode, build, master, blamelist)
        # bb3.5: create_context_for_build(mode, build, is_buildset, master, blamelist)
        if parse_version(buildbot_version) < parse_version("3.5"):
            ctx = message.create_context_for_build(mode, build, master, users)
        else:
            ctx = message.create_context_for_build(mode, build, is_buildset, master, users)

        # Additional fields.
        ctx["builder_url"]         = master.config.buildbotURL + f"#builders/{build['builder']['builderid']}"
        ctx["buildnumber"]         = build['number']
        ctx["buildreason"]         = build["buildset"]['reason'] if 'reason' in build["buildset"] else ''
        ctx["test_results"]        = build['test_results'] if 'test_results' in build else None
        ctx["artifacts"]           = build['artifacts'] if 'artifacts' in build else None
        ctx["lnt_reports"]         = build['lnt_reports'] if 'lnt_reports' in build else None

        msgdict = yield self.render_message_dict(master, ctx)
        return msgdict

    @defer.inlineCallbacks
    def render_message_dict(self, master, context):
        """
        Render a buildbot Slack message with attachments and return a tuple of message text
        and attachments.
        """
        text = ""

        try:
            yield self.buildAdditionalContext(master, context)

            buildername = context['buildername']
            emoji_code = self.getEmojiiForBuilder(buildername)
            builder_url = context['builder_url']
            build_url = context['build_url']
            results = context['results']

            started = context['mode'] is None

            if started:
                text = f":cyclone: Started build on <{builder_url}|{buildername}> builder."
                title = f"{context['projects']} / {buildername} / Build #{context['buildnumber']}"
            else:
                m_intro = get_intro_message(context['mode'], results, context['previous_results'])
                text = f"{m_intro} on <{builder_url}|{buildername}> builder."
                title = f"{emoji_code} {context['projects']} / {buildername} / Build #{context['buildnumber']}"

            debuglog(f"{self.name}: context : {context}")

            fields = []
            if not self.compact_message:
                short_fields = False

                fields.append(get_slack_attch_field(
                    title = "Build Reason",
                    value = context['buildreason'],
                    short = short_fields
                ))
                fields.append(get_slack_attch_field(
                    title = "Build Source Stamp",
                    value = context['sourcestamps'],
                    short = short_fields
                ))
                fields.append(get_slack_attch_field(
                    title = "Blamelist",
                    value = ", ".join(self.mapSlackUsersToBlamelist(context['blamelist'])),
                    short = short_fields
                ))
                if self.mentionUsers or self.mentionGroups:
                    fields.append(get_slack_attch_field(
                        title = "Mentions",
                        value = ", ".join(self.mentionUsers + self.mentionGroups),
                        short = short_fields
                    ))

            attachments = []

            if started:
                attachments.append(get_slack_attch(
                    fallback    = "Build status",
                    color       = get_result_color_code(SKIPPED),
                    title       = title,
                    title_link  = build_url,
                    text        = "",
                    fields      = fields
                ))
            else:
                attachments.append(get_slack_attch(
                    fallback    = "Build status",
                    color       = get_result_color_code(results),
                    title       = title,
                    title_link  = build_url,
                    text        = context['summary'],
                    fields      = fields
                ))

                #TODO:VV: implement if it is possible.
                #NOTE: the next few sections are not supported for now.
                # It needs some changes in the data model.

                # Test results.
                if context["test_results"] is not None:
                    for tests_status_result in context["test_results"]:
                        fields = []
                        count = tests_status_result["total"]
                        if count > 0:
                            fields.append(get_slack_attch_field(
                                title = "total tests",
                                value = count,
                                short = True
                            ))

                        for name in tests_status_result["results"]:
                            count = tests_status_result["results"][name]
                            fields.append(get_slack_attch_field(
                                title = name,
                                value = count,
                                short = True
                            ))

                        description = tests_status_result["description"]
                        if isinstance(description, list):
                            description = " ".join(description)
                        attachments.append(get_slack_attch(
                            fallback    = "Test Results",
                            pretext     = description,
                            color       = get_result_color_code(tests_status_result["status"]),
                            title       = f"{tests_status_result['name']} / Build #{context['buildnumber']}",
                            title_link  = f"{build_url}/steps/{ tests_status_result['name']}",
                            fields      = fields
                        ))

                # Artifact color: #CD853F, Peru
                if context['artifacts'] is not None:
                    for artifact_name, artifact_url in context['artifacts']:
                        attachments.append(get_slack_attch(
                            fallback    = "Build artifact",
                            pretext     = "Packages" if len(attachments) == 1 else None,
                            color       = get_artifact_color_code(),
                            title       = artifact_name,
                            title_link  = artifact_url
                        ))

                # Performance reports color: #6495ED, CornflowerBlue
                if context['lnt_reports'] is not None:
                    for lnt_report_status in context['lnt_reports']:
                        fallback_text = "LNT performance report submit"
                        if lnt_report_status["success"]:
                            attachments.append(get_slack_attch(
                                fallback    = fallback_text,
                                pretext     = None,
                                color       = "#6495ED",
                                title       = lnt_report_status["title"],
                                title_link  = lnt_report_status["url"]
                            ))
                        else:
                            # Failed report submit
                            # DarkRed:#8B0000
                            attachments.append(get_slack_attch(
                                fallback    = fallback_text,
                                pretext     = None,
                                color       = get_result_color_code(FAILURE),
                                title       = "Failed to submit the performance report.",
                                title_link  = None
                            ))

        except Exception as e:
            logging.msg(f"SlackMessageFormatter::render_message_dict: Exception: {e}")
            raise

        return {
            'type'          : self.template_type,
            'subject'       : text,
            'body'          : attachments,
        }


class SlackNotifier(ReporterBase):
    name = "SlackNotifier"
    #TODO:secrets = ["webhookurl"]

    def checkConfig(self, webhookurl, verbose=False,
                    debug=None, verify=None, generators=None,
                    **kwargs):

        if generators is None:
            generators = self._create_default_generators()

        super().checkConfig(generators=generators)
        httpclientservice.HTTPClientService.checkAvailable(self.__class__.__name__)

    @defer.inlineCallbacks
    def reconfigService(self, webhookurl, verbose=False,
                        debug=None, verify=None, generators=None,
                        **kwargs):
        #TODO:webhookurl = yield self.renderSecrets(webhookurl)
        self.debug = debug
        self.verify = verify
        self.verbose = verbose

        if generators is None:
            generators = self._create_default_generators()

        yield super().reconfigService(generators=generators)

        self.webhookurl = webhookurl
        self._http = yield httpclientservice.HTTPClientService.getService(
            self.master, self.webhookurl, debug=self.debug, verify=self.verify)

    def _create_default_generators(self):
        formatter = SlackMessageFormatter()
        return [
            BuildStatusGenerator(
                mode = 'all',
                message_formatter=formatter),
            WorkerMissingGenerator(
                workers='all',
                message_formatter=MessageFormatterMissingWorker())
            ]

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        ''' Send a text message to the slack channel.
            Method return a response object from the post method.
            Use r.text to get the response status.

            See more details here: https://api.slack.com/docs/messages/builder
        '''
        for report in reports:
            # NOTE: Because the default generators work only with a set of predefined fields,
            # we use 'body' to store the attachements and 'subject' to store the body of slack message.
            attch = report.get("body", None)
            body = report.get("subject", None)
            data = self.getPayload(body, None, attch)

            try:
                debuglog(f"{self.name}::send(): send slack message: data={json.dumps(data)}")
                response = yield self._http.post("", json=data)
                content = yield response.content()
                if not self.isStatus2XX(response.code):
                    logging.msg(f"{self.name}: status {response.code}: unable to send message: {content}")
                else:
                    debuglog(f"{self.name}::send(): response code={response.code}, content={content}")
            except Exception as e:
                logging.msg(f"{self.name}: failed to send status: {e}")


    def isStatus2XX(self, code):
        return code // 100 == 2

    def getPayload(self, text, subject = None, attachments = None):
        blocks = []
        if subject is not None:
            blocks.append(dict(
                type = "section",
                text = dict(
                    type = "mrkdwn",
                    text = subject
                )
            ))
        blocks.append(dict(
                type = "section",
                text = dict(
                    type = "mrkdwn",
                    text = text
                )
            ))

        payload = dict(
            blocks = blocks
        )

        if attachments is not None and len(attachments) > 0:
            payload['attachments'] = attachments

        return payload


# test when run directly
if __name__ == '__main__':
    pass
