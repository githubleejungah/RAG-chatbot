// 진입점: 구독 범위에서 리소스 그룹을 만들고 실제 리소스는 resources.bicep에 맡긴다.
// 값은 main.parameters.json에서 받으며, azd가 ${...} 자리표시자를 환경 값으로 바꾼다.
targetScope = 'subscription'

@description('azd 환경 이름. 리소스 그룹 이름과 고유 토큰에 사용한다.')
@minLength(1)
@maxLength(32)
param environmentName string

@description('모든 리소스의 지역.')
param location string

@description('리소스 이름 접두어. 소문자와 숫자만 사용한다.')
@minLength(2)
@maxLength(12)
param resourcePrefix string

@description('AI Search 등급. 비용 근거는 design/decisions.md D-09.')
@allowed([
  'free'
  'basic'
])
param searchSku string

@description('App Service Plan SKU. 근거는 design/decisions.md D-10.')
@allowed([
  'F1'
  'B1'
  'B2'
])
param appServiceSku string

@description('App Service의 Python 런타임.')
param pythonVersion string

@description('채팅 모델 이름. 근거는 design/decisions.md D-02.')
param chatModelName string

@description('채팅 모델 버전.')
param chatModelVersion string

@description('채팅 배포 용량(1,000 TPM 단위).')
@minValue(1)
param chatCapacity int

@description('임베딩 모델 이름. 근거는 design/decisions.md D-01.')
param embeddingModelName string

@description('임베딩 모델 버전.')
param embeddingModelVersion string

@description('임베딩 배포 용량(1,000 TPM 단위).')
@minValue(1)
param embeddingCapacity int

@description('벡터 인덱스 이름.')
param searchIndexName string

@description('근거 판단 임계치(@search.score). 보정 전까지 잠정값이다. design/decisions.md D-07.')
param minSearchScore string

@description('인덱싱을 실행할 운영자의 Entra 개체 ID. 비어 있으면 운영자 권한을 만들지 않는다.')
param principalId string = ''

@description('운영자 주체 형식.')
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param principalType string = 'User'

@description('/ask 호출용 API 키. 코드에 두지 않고 배포 때 전달하여 Key Vault에 저장한다.')
@secure()
@minLength(32)
param apiKey string

// 리소스 그룹: 실습 리소스를 한데 묶어 azd down으로 함께 지운다.
resource resourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: 'rg-${resourcePrefix}-${environmentName}'
  location: location
  tags: {
    'azd-env-name': environmentName
  }
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: resourceGroup
  params: {
    environmentName: environmentName
    location: location
    resourcePrefix: resourcePrefix
    searchSku: searchSku
    appServiceSku: appServiceSku
    pythonVersion: pythonVersion
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatCapacity: chatCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingCapacity: embeddingCapacity
    searchIndexName: searchIndexName
    minSearchScore: minSearchScore
    principalId: principalId
    principalType: principalType
    apiKey: apiKey
  }
}

// azd가 아래 출력을 환경 값으로 저장하므로 ingest.py와 smoke_test.py가 읽을 수 있다. 시크릿은 출력하지 않는다.
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint
output AZURE_OPENAI_CHAT_DEPLOYMENT string = resources.outputs.chatDeploymentName
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = resources.outputs.embeddingDeploymentName
output AZURE_SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output AZURE_SEARCH_INDEX string = searchIndexName
output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output SMOKE_BASE_URL string = resources.outputs.appUrl
