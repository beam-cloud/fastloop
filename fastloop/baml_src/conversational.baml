class ConversationHistory {
  role string
  content string
}

class ConversationalAgentInput {
  conversation_history ConversationHistory[]
  system_prompt string
  user_message string
}

function GenerateResponse(input: ConversationalAgentInput) -> string {
  client "openai/gpt-4o"
  prompt #"
    Make sure to always end your sentences with a period, question mark, or exclamation mark. You can have multiple sentences in one response.

    {{ input.system_prompt }}
    {{ input.conversation_history }}
    {{ input.user_message }}
  "#
}

test GenerateResponseTest {
  functions [GenerateResponse]
  args {
    input {
      conversation_history [
        { role "user", content "Question, have you seen the movie 'The Matrix'?" },
        { role "assistant", content "I'm sorry, I haven't seen it." },
        { role "user", content "That's too bad. Do you like sci-fi movies?" },
        { role "assistant", content "Yes, I do. My favorite sci-fi movie is 3-body problem." },
      ]
      system_prompt "You are a general conversational agent."
      user_message "Oh! Who is your favorite character?"
    }
  }
}