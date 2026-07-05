Steps followed for the project(june 26):


1. Ifc parsing extraction- only important elements related to the evacuation scenarios (done but needs check)
Important elements extracted required for evacuation scenarios:
ifcspace, ifcdoor, ifcslabs, ifcwalls, ifctransportelement, ifcrelspaceboundary, ifcwindows, escalators, elevators, ifcstairs, 

2. Hardcoded rules JSON regulation- based on UK Approved Document B Volume 1 (done but needs check)

3. Pre- check verifying the uk regulations of england, wales, northern ireland and scotland with the IFC elements extracted (done but needs check)

4. Scenario generation using LLMs with API KEY(NLP)  (still doing it)
- First test with free models during testing the system 
- Later use paid API key for testing the system
- code follows native sdk pattern

-backup = using langchain to make the LLM code more easier


implementation schema of using any LLM model: (19th june 26)
- call the API 
- define how the scenarios should be present
- finally calling and setting the token and temperature limit to avoid loosing api model limit.


making llm structured output steps: (23rd june 26)
from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate 
from pydantic import BaseModel 
from typing import List,Optional
from langchain_core.output_parsers import PydanticOutputParser


load_dotenv()

model= ChatMistralAI(model= 'mistral-small-2506')

#for json structured output schema using pydantic
class Movie(BaseModel):
   title: str
   release_year= Optional[int]
   genre: List[str]
   director: Optional[str]
   cast: List[str]
   rating: Optional[float]
   summary: str

prompt= ChatPromptTemplate.from_messages(
    ('system',""""
Extract movie information from the paragraph
    {format_ instructions}    
    """"),
    ("human","{paragraph}")

para = input("Give your paragraph:")

final_prompt= prompt.invoke(
    {"paragraph": para,
    'format_instructions':
    }
)

response= model.invoke(final_prompt)

)

#Similar example using pydantic langchain concept- 23rd june 26
import os
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# 1. Define the desired output data structure using Pydantic
class FireSafetyScenario(BaseModel):
    scenario_id: str = Field(description="Unique short code identifier for the test scenario.")
    title: str = Field(description="Clear name of the compliance edge case.")
    building_type: str = Field(description="The functional category of the asset (e.g., Residential Flats, Commercial Office).")
    description: str = Field(description="Detailed narrative of the structural arrangement and occupant conditions.")
    targeted_adb_regulations: list[str] = Field(description="List of Approved Document B unique regulation codes involved.")
    expected_violation_issue: str = Field(description="What specific failure the automated rule check should flag.")

# 2. Setup the LangChain components
def generate_compliance_scenarios(topic: str, total_count: int = 3):
    # Initialize the model (LangChain automatically looks for OPENAI_API_KEY environment variable)
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.7  # Higher temperature gives more creative variation in scenarios
    )

    # Initialize the JSON parser with our Pydantic schema structure
    output_parser = JsonOutputParser(pydantic_object=FireSafetyScenario)

    # 3. Design a targeted prompt template
    prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "You are an expert structural fire safety engineer specializing in the England Building Regulations (Approved Document B).\n"
            "Generate realistic, challenging synthetic edge-case test scenarios that developers can use to validate automated IFC file rule-checking scripts.\n"
            "You must strictly output your response matching the following JSON formatting schema:\n{format_instructions}"
        )),
        ("human", "Generate {count} distinct safety scenario profiles regarding this theme: {theme}")
    ])

    # Inject the exact formatting instructions the parser expects into the prompt
    prompt_with_instructions = prompt.partial(format_instructions=output_parser.get_format_instructions())

    # 4. Construct the LCEL Chain (Prompt -> LLM -> Parser)
    # The '|' operator chains these distinct programmatic links together natively.
    chain = prompt_with_instructions | llm | output_parser

    # 5. Invoke the chain execution pass
    try:
        response_data = chain.invoke({
            "theme": topic,
            "count": total_count
        })
        return response_data
    except Exception as e:
        print(f"Pipeline execution anomaly: {e}")
        return None

# ==========================================
# RUNTIME INVOCATION EXECUTION
# ==========================================
if __name__ == "__main__":
    # Ensure your API key is configured in your environment
    # os.environ["OPENAI_API_KEY"] = "your-api-key-here"

    test_topic = "Corridors serving flats with sub-standard clear escape route widths and missing fire-rated door separations"
    print(f"🚀 Launching scenario generation chain for theme: '{test_topic}'...\n")
    
    generated_scenarios = generate_compliance_scenarios(topic=test_topic, total_count=2)
    
    if generated_scenarios:
        import json
        print("🎯 Generated Synthetic Engineering Scenarios:")
        print(json.dumps(generated_scenarios, indent=2))


5. export the results into a JSON structure(directly in streamlit)

6. Build the User interface using Streamlit(final)
- backup - using streamlit(connection and web design) instead of flask with html, css and javascript if time is less 

7. Run and test the system if it gives the scenarios or not and fix the codes wherever possible(2nd week of july)

Target- finish the codes by 15th july(both backend and frontend)
 
