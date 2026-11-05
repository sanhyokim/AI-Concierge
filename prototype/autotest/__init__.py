"""The caller-role test (客役の自動試験). User, 2026-11-05: 「これらの検証をあなたが客のスタンスで多様なパターンを検証して
くれることはできないのですか？」.

A caller AI plays each pattern of prototype/tenants/<company>.callers.json (a person, what they know, how they
talk) and talks in text with the reception AI: the ElevenAgents agent in text only (chat mode), with the same
instructions, tools and thinking AI as the voice test, and the same reception service behind the tools. After each
conversation the server checks what the pattern expects (the number confirmed, the count asked, no words the AI
must not use, ...) and keeps the record like any other test call.

It tests the conversation and the reception logic, not the voice: mishearing, pronunciation and pauses are only
found in the voice test. It runs on the tester's PC with the tester's keys (paid APIs: ElevenLabs per message and
the thinking AI; OpenAI for the caller AI), when the tester presses the button on the test page.
"""
