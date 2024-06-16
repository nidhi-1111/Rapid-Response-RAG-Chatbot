import os
import streamlit as st
import pdfplumber
from langchain_core.runnables import (
    RunnableBranch,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts.prompt import PromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Field
from typing import Tuple, List, Optional
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
import os
from dotenv import load_dotenv

from langchain_community.graphs import Neo4jGraph
from langchain.document_loaders import WikipediaLoader
from langchain.text_splitter import TokenTextSplitter
from langchain_openai import ChatOpenAI
from langchain_experimental.graph_transformers import LLMGraphTransformer
from neo4j import GraphDatabase
from yfiles_jupyter_graphs import GraphWidget
from langchain_community.vectorstores import Neo4jVector
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores.neo4j_vector import remove_lucene_chars
from langchain_core.runnables import ConfigurableField, RunnableParallel, RunnablePassthrough
# from langchain.document_loaders import TextLoader
from langchain.docstore.document import Document


load_dotenv()
groq_api_key = os.getenv('GROQ_API_KEY')
pinecone_api_key = os.getenv('PINECONE_API_KEY')w
openai_api_key = os.getenv('OPENAI_API_KEY')
# google_api_key = os.getenv('GOOGLE_API_KEY')
neo4j_uri = os.getenv('NEO4J_URI')
neo4j_username = os.getenv('NEO4J_USERNAME')
neo4j_pass = os.getenv('NEO4J_PASSWORD')
graph = Neo4jGraph()



st.set_page_config(page_title="Knowledge Graph Builder")



def load_pdf(file):
    with pdfplumber.open(file) as pdf:
        text = ""
        for page in pdf.pages:
            text += page.extract_text()
    return text

def main():
    st.title("Knowledge Graph Builder")
    
    # Upload PDF file
    uploaded_file = st.file_uploader("Upload a PDF file", type="pdf")
    
    if uploaded_file is not None:
        # Load PDF content
        pdf_text = load_pdf(uploaded_file)
        
        # Split PDF content into chunks
        text_splitter = TokenTextSplitter(chunk_size=512, chunk_overlap=24)
        text_chunks = text_splitter.split_text(pdf_text)
        documents = [Document(page_content=chunk) for chunk in text_chunks]


        llm = ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo-0125")  # gpt-4-0125-preview occasionally has issues
        llm_transformer = LLMGraphTransformer(llm=llm)

        graph_documents = llm_transformer.convert_to_graph_documents(documents)
        # graph = llm_transformer.graph
 

        graph.add_graph_documents(
            graph_documents,
            baseEntityLabel=True,
            include_source=True
        )

        default_cypher = "MATCH (s)-[r:!MENTIONS]->(t) RETURN s,r,t LIMIT 50"

        def showGraph(cypher: str = default_cypher):
            # create a neo4j session to run queries
            driver = GraphDatabase.driver(
                uri=os.environ["NEO4J_URI"],
                auth=(os.environ["NEO4J_USERNAME"],
                      os.environ["NEO4J_PASSWORD"]))
            session = driver.session()
            widget = GraphWidget(graph=session.run(cypher).graph())
            widget.node_label_mapping = 'id'
            return widget

        vector_index = Neo4jVector.from_existing_graph(
            OpenAIEmbeddings(),
            search_type="hybrid",
            node_label="Document",
            text_node_properties=["text"],
            embedding_node_property="embedding"
        )

        # Retriever

        graph.query(
            "CREATE FULLTEXT INDEX entity IF NOT EXISTS FOR (e:__Entity__) ON EACH [e.id]")

        # Extract entities from text
        class Entities(BaseModel):
            """Identifying information about entities."""

            names: List[str] = Field(
                ...,
                description="All the person, organization, or business entities that "
                "appear in the text",
            )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are extracting organization and person entities from the text.",
                ),
                (
                    "human",
                    "Use the given format to extract information from the following "
                    "input: {question}",
                ),
            ]
        )

        entity_chain = prompt | llm.with_structured_output(Entities)

        def generate_full_text_query(input: str) -> str:
            """
            Generate a full-text search query for a given input string.

            This function constructs a query string suitable for a full-text search.
            It processes the input string by splitting it into words and appending a
            similarity threshold (~2 changed characters) to each word, then combines
            them using the AND operator. Useful for mapping entities from user questions
            to database values, and allows for some misspelings.
            """
            full_text_query = ""
            words = [el for el in remove_lucene_chars(input).split() if el]
            for word in words[:-1]:
                full_text_query += f" {word}~2 AND"
            full_text_query += f" {words[-1]}~2"
            return full_text_query.strip()

        # Fulltext index query
        def structured_retriever(question: str) -> str:
            """
            Collects the neighborhood of entities mentioned
            in the question
            """
            result = ""
            entities = entity_chain.invoke({"question": question})
            for entity in entities.names:
                response = graph.query(
                    """CALL db.index.fulltext.queryNodes('entity', $query, {limit:2})
                    YIELD node,score
                    CALL {
                      WITH node
                      MATCH (node)-[r:!MENTIONS]->(neighbor)
                      RETURN node.id + ' - ' + type(r) + ' -> ' + neighbor.id AS output
                      UNION ALL
                      WITH node
                      MATCH (node)<-[r:!MENTIONS]-(neighbor)
                      RETURN neighbor.id + ' - ' + type(r) + ' -> ' +  node.id AS output
                    }
                    RETURN output LIMIT 50
                    """,
                    {"query": generate_full_text_query(entity)},
                )
                result += "\n".join([el['output'] for el in response])
            return result

        def retriever(question: str):
            print(f"Search query: {question}")
            structured_data = structured_retriever(question)
            unstructured_data = [el.page_content for el in vector_index.similarity_search(question)]
            final_data = f"""Structured data:
                            {structured_data}
                            Unstructured data:
                            {"#Document ". join(unstructured_data)}
                                        """
            return final_data

        # Condense a chat history and follow-up question into a standalone question
        _template = """Given the following conversation and a follow up question, rephrase the follow up question to be a standalone question,
                        in its original language.
                        Chat History:
                        {chat_history}
                        Follow Up Input: {question}
                        Standalone question:"""  # noqa: E501
        CONDENSE_QUESTION_PROMPT = PromptTemplate.from_template(_template)

        def _format_chat_history(chat_history: List[Tuple[str, str]]) -> List:
            buffer = []
            for human, ai in chat_history:
                buffer.append(HumanMessage(content=human))
                buffer.append(AIMessage(content=ai))
            return buffer

        _search_query = RunnableBranch(
            # If input includes chat_history, we condense it with the follow-up question
            (
                RunnableLambda(lambda x: bool(x.get("chat_history"))).with_config(
                    run_name="HasChatHistoryCheck"
                ),  # Condense follow-up question and chat into a standalone_question
                RunnablePassthrough.assign(
                    chat_history=lambda x: _format_chat_history(x["chat_history"])
                )
                | CONDENSE_QUESTION_PROMPT
                | ChatOpenAI(temperature=0)
                | StrOutputParser(),
            ),
            # Else, we have no chat history, so just pass through the question
            RunnableLambda(lambda x: x["question"]),
        )

        template = """Answer the question based only on the following context:
                    {context}

                    Question: {question}
                    Answer:"""
        prompt = ChatPromptTemplate.from_template(template)

        chain = (
            RunnableParallel(
                {
                    "context": _search_query | retriever,
                    "question": RunnablePassthrough(),
                }
            )
            | prompt
            | llm
            | StrOutputParser()
        )

        # # You can test the chain with sample questions
        # chain.invoke({"question": "Which house did Elizabeth I belong to?"})

        # chain.invoke(
        #     {
        #         "question": "When was she born?",
        #         "chat_history": [("Which house did Elizabeth I belong to?", "House Of Tudor")],
        #     }
        # )
        user_question = st.text_input("Ask a question about the PDF content:")

        if user_question:
            answer = chain.invoke({"question": user_question})
            st.write(f"Answer: {answer}")


if __name__ == "__main__":
    main()


# # Read the wikipedia article
# raw_documents = WikipediaLoader(query="Elizabeth I").load()
# # Define chunking strategy
# text_splitter = TokenTextSplitter(chunk_size=512, chunk_overlap=24)
# documents = text_splitter.split_documents(raw_documents[:3])

# llm=ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo-0125") # gpt-4-0125-preview occasionally has issues
# llm_transformer = LLMGraphTransformer(llm=llm)

# graph_documents = llm_transformer.convert_to_graph_documents(documents)
# graph.add_graph_documents(
#     graph_documents,
#     baseEntityLabel=True,
#     include_source=True
# )


# default_cypher = "MATCH (s)-[r:!MENTIONS]->(t) RETURN s,r,t LIMIT 50"

# def showGraph(cypher: str = default_cypher):
#     # create a neo4j session to run queries
#     driver = GraphDatabase.driver(
#         uri = os.environ["NEO4J_URI"],
#         auth = (os.environ["NEO4J_USERNAME"],
#                 os.environ["NEO4J_PASSWORD"]))
#     session = driver.session()
#     widget = GraphWidget(graph = session.run(cypher).graph())
#     widget.node_label_mapping = 'id'
#     #display(widget)
#     return widget

# vector_index = Neo4jVector.from_existing_graph(
#     OpenAIEmbeddings(),
#     search_type="hybrid",
#     node_label="Document",
#     text_node_properties=["text"],
#     embedding_node_property="embedding"
# )

# # Retriever

# graph.query(
#     "CREATE FULLTEXT INDEX entity IF NOT EXISTS FOR (e:__Entity__) ON EACH [e.id]")

# # Extract entities from text
# class Entities(BaseModel):
#     """Identifying information about entities."""

#     names: List[str] = Field(
#         ...,
#         description="All the person, organization, or business entities that "
#         "appear in the text",
#     )

# prompt = ChatPromptTemplate.from_messages(
#     [
#         (
#             "system",
#             "You are extracting organization and person entities from the text.",
#         ),
#         (
#             "human",
#             "Use the given format to extract information from the following "
#             "input: {question}",
#         ),
#     ]
# )

# entity_chain = prompt | llm.with_structured_output(Entities)

# def generate_full_text_query(input: str) -> str:
#     """
#     Generate a full-text search query for a given input string.

#     This function constructs a query string suitable for a full-text search.
#     It processes the input string by splitting it into words and appending a
#     similarity threshold (~2 changed characters) to each word, then combines
#     them using the AND operator. Useful for mapping entities from user questions
#     to database values, and allows for some misspelings.
#     """
#     full_text_query = ""
#     words = [el for el in remove_lucene_chars(input).split() if el]
#     for word in words[:-1]:
#         full_text_query += f" {word}~2 AND"
#     full_text_query += f" {words[-1]}~2"
#     return full_text_query.strip()

# # Fulltext index query
# def structured_retriever(question: str) -> str:
#     """
#     Collects the neighborhood of entities mentioned
#     in the question
#     """
#     result = ""
#     entities = entity_chain.invoke({"question": question})
#     for entity in entities.names:
#         response = graph.query(
#             """CALL db.index.fulltext.queryNodes('entity', $query, {limit:2})
#             YIELD node,score
#             CALL {
#               WITH node
#               MATCH (node)-[r:!MENTIONS]->(neighbor)
#               RETURN node.id + ' - ' + type(r) + ' -> ' + neighbor.id AS output
#               UNION ALL
#               WITH node
#               MATCH (node)<-[r:!MENTIONS]-(neighbor)
#               RETURN neighbor.id + ' - ' + type(r) + ' -> ' +  node.id AS output
#             }
#             RETURN output LIMIT 50
#             """,
#             {"query": generate_full_text_query(entity)},
#         )
#         result += "\n".join([el['output'] for el in response])
#     return result


# def retriever(question: str):
#     print(f"Search query: {question}")
#     structured_data = structured_retriever(question)
#     unstructured_data = [el.page_content for el in vector_index.similarity_search(question)]
#     final_data = f"""Structured data:
# {structured_data}
# Unstructured data:
# {"#Document ". join(unstructured_data)}
#     """
#     return final_data

# # Condense a chat history and follow-up question into a standalone question
# _template = """Given the following conversation and a follow up question, rephrase the follow up question to be a standalone question,
# in its original language.
# Chat History:
# {chat_history}
# Follow Up Input: {question}
# Standalone question:"""  # noqa: E501
# CONDENSE_QUESTION_PROMPT = PromptTemplate.from_template(_template)

# def _format_chat_history(chat_history: List[Tuple[str, str]]) -> List:
#     buffer = []
#     for human, ai in chat_history:
#         buffer.append(HumanMessage(content=human))
#         buffer.append(AIMessage(content=ai))
#     return buffer

# _search_query = RunnableBranch(
#     # If input includes chat_history, we condense it with the follow-up question
#     (
#         RunnableLambda(lambda x: bool(x.get("chat_history"))).with_config(
#             run_name="HasChatHistoryCheck"
#         ),  # Condense follow-up question and chat into a standalone_question
#         RunnablePassthrough.assign(
#             chat_history=lambda x: _format_chat_history(x["chat_history"])
#         )
#         | CONDENSE_QUESTION_PROMPT
#         | ChatOpenAI(temperature=0)
#         | StrOutputParser(),
#     ),
#     # Else, we have no chat history, so just pass through the question
#     RunnableLambda(lambda x : x["question"]),
# )



# template = """Answer the question based only on the following context:
# {context}

# Question: {question}
# Use natural language and be concise.
# Answer:"""
# prompt = ChatPromptTemplate.from_template(template)

# chain = (
#     RunnableParallel(
#         {
#             "context": _search_query | retriever,
#             "question": RunnablePassthrough(),
#         }
#     )
#     | prompt
#     | llm
#     | StrOutputParser()
# )

# chain.invoke({"question": "Which house did Elizabeth I belong to?"})


# chain.invoke(
#     {
#         "question": "When was she born?",
#         "chat_history": [("Which house did Elizabeth I belong to?", "House Of Tudor")],
#     }
# )

