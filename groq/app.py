import streamlit as st
import os
from langchain_groq import ChatGroq
from langchain_community.document_loaders import WebBaseLoader
# from langchain_community.embeddings import OllamaEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain.chains import create_retrieval_chain
from langchain_community.vectorstores.faiss import FAISS

import time

from dotenv import load_dotenv
load_dotenv()

## Load the Groq API key
groq_api_key = os.environ['GROQ_API_KEY']
google_api_key = os.environ['GOOGLE_API_KEY']

st.title("Chat with Groq and Mixtral")
website_link = st.text_input("Enter the website link:")

# if "vector" not in st.session_state:
if website_link:
    # st.session_state.embeddings = OllamaEmbeddings()
    st.session_state.embeddings =GoogleGenerativeAIEmbeddings(model = 'models/embedding-001',google_api_key=google_api_key)
    st.session_state.loader = WebBaseLoader(website_link)
    st.session_state.docs = st.session_state.loader.load()

    st.session_state.text_splitter = RecursiveCharacterTextSplitter(chunk_size =1000, chunk_overlap= 200)
    st.session_state.final_documents = st.session_state.text_splitter.split_documents(st.session_state.docs[:50])
    st.session_state.vector = FAISS.from_documents(st.session_state.final_documents,st.session_state.embeddings)
    llm = ChatGroq(groq_api_key=groq_api_key, model="mixtral-8x7b-32768")
    # mixtral-8x7b-32768
    prompt = ChatPromptTemplate.from_template(
    """
    Answer the question based on the provided context only.
    Please provide the most accurate response based on the question
    <context>
    {context}
    </context>
    Questions:{input}
    """
    )
    document_chain = create_stuff_documents_chain(llm,prompt)
    retriever = st.session_state.vector.as_retriever()
    retrieval_chain = create_retrieval_chain(retriever,document_chain)

    prompt = st.text_input("Input your prompt here")

# st.title("Chat with Groq and Mixtral")
    if prompt:
        # llm = ChatGroq(groq_api_key=groq_api_key, model="mixtral-8x7b-32768")
        # # mixtral-8x7b-32768
        # prompt = ChatPromptTemplate.from_template(
        # """
        # Answer the question based on the provided context only.
        # Please provide the most accurate response based on the question
        # <context>
        # {context}
        # </context>
        # Questions:{input}
        # """
        # )
        # document_chain = create_stuff_documents_chain(llm,prompt)
        # retriever = st.session_state.vector.as_retriever()
        # retrieval_chain = create_retrieval_chain(retriever,document_chain)

        # if prompt:
        start =time.process_time()
        response = retrieval_chain.invoke({"input":prompt})
        print("Response time :", time.process_time()-start)
        st.write(response['answer'])

        with st.expander("Document Similarity Search"):
            for i, doc in enumerate(response['context']):
                st.write(doc.page_content)
                st.write("-----------------------------")
