FROM python:3.12-slim

WORKDIR /app

# Install ODBC Driver 17 for SQL Server + Azure CLI (for DefaultAzureCredential)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg apt-transport-https unixodbc-dev ca-certificates lsb-release && \
    curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - && \
    curl https://packages.microsoft.com/config/debian/11/prod.list \
        > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql17 && \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app/src

# In Azure Container Apps / AKS: DefaultAzureCredential picks up Managed Identity
# automatically. No LLM_API_KEY needed when running in Azure.
CMD ["python", "main.py"]
