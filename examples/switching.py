from fastloop import FastLoop, LoopContext, LoopEvent

app = FastLoop(name="research-agent")


@app.event("research_topic")
class ResearchTopic(LoopEvent):
    topic: str


@app.event("research_result")
class ResearchResult(LoopEvent):
    result: str


class ResearchAgent:
    def __init__(self, context: LoopContext):
        pass

    def add_research(self, context: LoopContext):
        pass

    def get_research(self, context: LoopContext):
        return context.switch_to(self.add_research)


# app.loop(
#     name="research",
#     start_event=ResearchTopic,
# ).from_class(ResearchAgent)
