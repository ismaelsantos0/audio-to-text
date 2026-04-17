# 1. Define a imagem base do Python (usando a versão 3.10 slim para economizar espaço)
FROM python:3.10-slim

# 2. Define a pasta de trabalho dentro do servidor
WORKDIR /app

# 3. Instala o FFmpeg, o Git e limpa o cache do apt para economizar espaço
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*

# 4. Copia o arquivo de dependências para o servidor
COPY requirements.txt .

# 5. Atualiza o pip e instala as dependências
RUN pip install --upgrade pip setuptools wheel
RUN pip install -r requirements.txt

# 6. Copia o resto do código do seu bot para o servidor
COPY . .

# 7. Comando para iniciar o bot (se o seu arquivo não chamar main.py, mude aqui)
CMD ["python", "main.py"]
