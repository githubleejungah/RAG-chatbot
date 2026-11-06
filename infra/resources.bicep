// 리소스 그룹 범위의 실제 리소스. 구성과 권한은 design/architecture.md 3절을 따른다.
targetScope = 'resourceGroup'

param environmentName string
param location string
param resourcePrefix string
param searchSku string
param appServiceSku string
param pythonVersion string
param chatModelName string
param chatModelVersion string
param chatCapacity int
param embeddingModelName string
param embeddingModelVersion string
param embeddingCapacity int
param searchIndexName string
param minSearchScore string
param principalId string
param principalType string

@secure()
param apiKey string

// 구독과 환경마다 같은 값이 나오는 토큰으로 전역 고유 이름을 만든다.
var token = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
}
var apiKeySecretName = 'api-key'

// 기본 제공 역할 ID. Microsoft Learn의 Azure built-in roles 문서에서 확인했다.
var roles = {
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

// AI Search: 청크와 벡터를 저장하고 최근접 청크를 반환한다. 로컬 인증을 꺼서 RBAC로만 접근한다.
resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'srch-${resourcePrefix}-${token}'
  location: location
  tags: tags
  sku: {
    name: searchSku
  }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'enabled'
  }
}

// Azure OpenAI 계정: 임베딩과 채팅 생성을 제공한다. 토큰 인증에 사용자 지정 도메인이 필요하고, 키 인증은 끈다.
resource openAi 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'oai-${resourcePrefix}-${token}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: 'oai-${resourcePrefix}-${token}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

// 임베딩 배포: 질문과 청크를 벡터로 변환한다. 인덱싱과 질의에 같은 배포를 쓴다.
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: openAi
  name: embeddingModelName
  sku: {
    name: 'GlobalStandard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// 채팅 배포: 근거로 답변을 생성한다. 한 계정의 배포는 동시에 만들 수 없어 임베딩 배포 뒤에 만든다.
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: openAi
  name: chatModelName
  sku: {
    name: 'GlobalStandard'
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    embeddingDeployment
  ]
}

// Key Vault: /ask 호출용 API 키를 보관한다. 접근 권한은 RBAC로 관리한다.
resource keyVault 'Microsoft.KeyVault/vaults@2026-02-01' = {
  name: 'kv-${take(resourcePrefix, 6)}-${take(token, 13)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// API 키 시크릿: 값은 배포 때 보안 파라미터로만 전달된다.
resource apiKeySecret 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = {
  parent: keyVault
  name: apiKeySecretName
  properties: {
    value: apiKey
  }
}

// App Service Plan: FastAPI가 실행되는 Linux 컴퓨팅.
resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: 'plan-${resourcePrefix}-${token}'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: appServiceSku
  }
  properties: {
    reserved: true
  }
}

// App Service: /ask, /health를 제공한다. 시스템 할당 ID로 키 없이 다른 리소스에 접근한다.
// azd-service-name 태그가 azure.yaml의 api 서비스와 이 앱을 연결한다.
resource app 'Microsoft.Web/sites@2025-03-01' = {
  name: 'app-${resourcePrefix}-${token}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      // F1은 Always On을 지원하지 않아 true면 배포가 실패한다.
      alwaysOn: appServiceSku != 'F1'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appCommandLine: 'gunicorn -w 2 -k uvicorn.workers.UvicornWorker --timeout 60 --bind=0.0.0.0:8000 main:app'
      appSettings: [
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'LLM_PROVIDER'
          value: 'azure'
        }
        {
          name: 'AZURE_OPENAI_ENDPOINT'
          value: openAi.properties.endpoint
        }
        {
          name: 'AZURE_OPENAI_CHAT_DEPLOYMENT'
          value: chatDeployment.name
        }
        {
          name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT'
          value: embeddingDeployment.name
        }
        {
          name: 'AZURE_SEARCH_ENDPOINT'
          value: 'https://${search.name}.search.windows.net'
        }
        {
          name: 'AZURE_SEARCH_INDEX'
          value: searchIndexName
        }
        {
          name: 'MIN_SEARCH_SCORE'
          value: minSearchScore
        }
        {
          // 값 대신 Key Vault 참조만 설정한다. 앱의 관리 ID로 플랫폼이 값을 읽어 온다.
          name: 'API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=${apiKeySecretName})'
        }
      ]
    }
  }
  dependsOn: [
    apiKeySecret
  ]
}

// 앱 권한: 인덱스 조회만 허용한다. 인덱스 변경 권한은 주지 않는다.
resource appSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, app.id, roles.searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// 앱 권한: 임베딩과 채팅 호출.
resource appOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, app.id, roles.openAiUser)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// 앱 권한: Key Vault 참조로 API 키 읽기.
resource appKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, app.id, roles.keyVaultSecretsUser)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// 운영자 권한: ingest.py의 인덱스 생성.
resource operatorSearchContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, roles.searchServiceContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: principalId
    principalType: principalType
  }
}

// 운영자 권한: ingest.py의 문서 업로드, calibrate.py의 조회.
resource operatorSearchDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, roles.searchIndexDataContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
    principalId: principalId
    principalType: principalType
  }
}

// 운영자 권한: ingest.py와 calibrate.py의 임베딩 호출.
resource operatorOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(openAi.id, principalId, roles.openAiUser)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: principalId
    principalType: principalType
  }
}

output openAiEndpoint string = openAi.properties.endpoint
output chatDeploymentName string = chatDeployment.name
output embeddingDeploymentName string = embeddingDeployment.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output keyVaultName string = keyVault.name
output appUrl string = 'https://${app.properties.defaultHostName}'
