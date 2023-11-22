from __future__ import annotations

import uuid
import logging
import warnings
import psycopg2
from psycopg2 import sql

from itertools import repeat

from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
)

from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore
from langchain.docstore.document import Document

logger = logging.getLogger(__name__)

class YellowbrickVector(VectorStore):
    """Wrapper around Yellowbrick as a vector database.
    Example:
        .. code-block:: python
            from langchain.vectorstores import YellowbrickVector
            from langchain.embeddings.openai import OpenAIEmbeddings
            ...
    """

    def __init__(
        self,
        embedding: Embeddings,
        connection_string: str,
        table: str,
        connection: Optional[psycopg2.connection] = None,
    ) -> None:
        """Initialize with yellowbrick client."""

        if not isinstance(embedding, Embeddings):
            warnings.warn("embeddings input must be Embeddings object.")

        self.connection_string = connection_string
        self._table = table
        self._embedding = embedding
        # Create a connection if not provided, otherwise use the provided connection
        self._connection = connection if connection else self.create_connection(connection_string)
        self.__post_init__()

    def __post_init__(
        self,
    ) -> None:
        """Initialize the store."""
        self.check_database_utf8()
        self.create_table_if_not_exists()

    def __del__(self) -> None:
        if self._connection:
            self._connection.close()

    def create_table_if_not_exists(self) -> None:
        cursor = self._connection.cursor()
        cursor.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} ( \
            id UUID, embedding_id INTEGER, text VARCHAR(60000), metadata VARCHAR(1024), \
            embedding FLOAT)').format(sql.Identifier(self._table)))
        self._connection.commit()
        cursor.close()

    def drop(self,
             table: str) -> None:
        """
        Helper function: Drop data
        """
        cursor = self._connection.cursor()
        cursor.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(sql.Identifier(table)))
        self._connection.commit()
        cursor.close()

    def check_database_utf8(self):
        """
        Helper function: Test the database is UTF-8 encoded
        """
        cursor = self._connection.cursor()
        query = "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database();"
        cursor.execute(query)
        encoding = cursor.fetchone()[0]
        cursor.close()
        if encoding.lower() == 'utf8' or encoding.lower() == 'utf-8':
            return True
        else:
            raise Exception(f"Database '{self.connection_string.split('/')[-1]}' encoding is not UTF-8")

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Add more texts to the vectorstore index.
        Args:
            texts: Iterable of strings to add to the vectorstore.
            metadatas: Optional list of metadatas associated with the texts.
            kwargs: vectorstore specific parameters
        """
        cursor = self._connection.cursor()
        embeddings = self._embedding.embed_documents(list(texts))
        results = []
        if not metadatas:
            metadatas = [{} for _ in texts]
        for id in range(len(embeddings)):
            doc_uuid = uuid.uuid4()
            results.append(doc_uuid)
            data_input = [
                (str(id), embedding_id, text, json.dumps(metadata), embedding)
                for id, embedding_id, text, metadata, embedding in zip(repeat(doc_uuid), range(len(embeddings[id])), repeat(texts[id]), repeat(metadatas[id]), embeddings[id])
            ]
            data_input = [val for sublist in data_input for val in sublist]
            insert_query = sql.SQL('INSERT INTO {t} (id, embedding_id, text, metadata, embedding) VALUES {v}').format(t=sql.Identifier(self._table),v=(sql.SQL(',').join([sql.SQL('(%s,%s,%s,%s,%s)') for _ in range(len(embeddings[id]))]))
            )
            cursor.execute(insert_query, data_input)
            self._connection.commit()
        return results

    @classmethod
    def from_texts(
        cls: Type[YellowbrickVector],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        connection_string: str = "",
        table: str = "langchain",
        **kwargs: Any,
    ) -> YellowbrickVector:
        if connection_string is None:
            raise ValueError("connection_string must be provided")
        vss = cls(
            embedding=embedding,
            connection_string=connection_string,
            table=table,
        )
        vss.add_texts(texts=texts, metadatas=metadatas)
        return vss

    def similarity_search_with_score_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        cursor = self._connection.cursor()
        tmp_table = "tmp_" + self._table
        cursor.execute(sql.SQL('CREATE TEMPORARY TABLE {} ( \
            embedding_id INTEGER, embedding FLOAT)').format(sql.Identifier(tmp_table)))
        self._connection.commit()

        data_input = [
            (embedding_id, embedding)
            for embedding_id, embedding in zip(range(len(embedding)), embedding)
        ]
        data_input = [val for sublist in data_input for val in sublist]
        insert_query = sql.SQL('INSERT INTO {t} (embedding_id, embedding) VALUES {v}').format(
            t=sql.Identifier(tmp_table), v=sql.SQL(',').join([sql.SQL('(%s,%s)') for _ in range(len(embedding))])
        )
        cursor.execute(insert_query, data_input)
        self._connection.commit()
        sql_query = sql.SQL('SELECT text, \
            metadata, \
            sum(v1.embedding * v2.embedding) / \
            ( sqrt(sum(v1.embedding * v1.embedding)) * sqrt(sum(v2.embedding * v2.embedding))) AS score \
            FROM {v1} v1 INNER JOIN {v2} v2 \
            ON v1.embedding_id = v2.embedding_id \
            GROUP BY v2.id, v2.text, v2.metadata \
            ORDER BY score DESC \
            LIMIT %s').format(v1=sql.Identifier(tmp_table), v2=sql.Identifier(self._table))
        cursor.execute(sql_query, (k,))
        results = cursor.fetchall()
        self.drop(tmp_table)

        documents = []
        for row in results:
            metadata = json.loads(row[1]) or {}
            doc = Document(page_content=row[0], metadata=metadata)
            documents.append((doc, row[2]))

        cursor.close()
        return documents

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        """Return docs most similar to query."""
        embedding = self._embedding.embed_query(query)
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k
        )
        return [doc for doc, _ in documents]

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """Return docs most similar to query."""
        embedding = self._embedding.embed_query(query)
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k
        )
        return documents

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Document]:
        documents = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k
        )
        return [doc for doc, _ in documents]

    @staticmethod
    def create_connection(connection_string: str) -> psycopg2.connection:
        # connection string: psycopg2.connect('postgres://username:password@host:port/database')
        connection = psycopg2.connect(connection_string)
        return connection
