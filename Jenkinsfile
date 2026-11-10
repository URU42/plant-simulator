// plant-simulator CI/CD: 테스트 → 이미지 빌드 → Harbor push → (선택) k8s 배포.
//
// 전제와 바꿀 값
// - 에이전트: Docker CLI 와 Python 3.11 + uv 가 있는 노드(Docker-in-Docker 는 클러스터 정책상 금지이므로 Jenkins 는 클러스터 밖이거나 docker 소켓이 있는 노드).
//   필요하면 agent { label 'docker' } 처럼 레이블을 지정한다.
// - 자격 증명(Jenkins Credentials):
//     harbor-cred      Username with password — Harbor(harbor.uru42.space) 로그인
//     kubeconfig-u4-2  Secret file — 네임스페이스 u4-2 에 apply 할 kubeconfig (DEPLOY=true 일 때만 필요)
// - 이미지: harbor.uru42.space/uru42/plant-simulator:<빌드번호>-<커밋7자리> 와 :latest
// - 배포: 파라미터 DEPLOY 를 켰을 때만 kustomize 로 태그를 바꿔 kubectl apply -k deploy/k8s -n u4-2.
//   첫 기동은 다섯 공장 하루 백필(500m CPU 에서 약 15분)이라 rollout 대기는 30분까지 둔다.

pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    parameters {
        booleanParam(name: 'DEPLOY', defaultValue: false, description: 'Harbor push 뒤 u4-2 네임스페이스에 배포한다')
        booleanParam(name: 'PUSH_LATEST', defaultValue: true, description: ':latest 태그도 함께 push 한다')
    }

    environment {
        REGISTRY   = 'harbor.uru42.space'
        PROJECT    = 'uru42'
        IMAGE      = "${REGISTRY}/${PROJECT}/plant-simulator"
        NAMESPACE  = 'u4-2'
        PLATFORM   = 'linux/amd64'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    env.GIT_SHORT = sh(script: 'git rev-parse --short=7 HEAD', returnStdout: true).trim()
                    env.TAG = "${env.BUILD_NUMBER}-${env.GIT_SHORT}"
                }
                echo "image tag: ${env.TAG}"
            }
        }

        stage('Test') {
            steps {
                sh '''
                    uv sync --frozen
                    uv run pytest -q -p no:cacheprovider --junitxml=reports/pytest.xml
                '''
            }
            post {
                always { junit allowEmptyResults: true, testResults: 'reports/pytest.xml' }
            }
        }

        stage('Build image') {
            steps {
                sh "docker build --platform ${PLATFORM} -t ${IMAGE}:${TAG} ."
            }
        }

        stage('Push to Harbor') {
            steps {
                withCredentials([usernamePassword(credentialsId: 'harbor-cred', usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASS')]) {
                    sh '''
                        echo "$HARBOR_PASS" | docker login ${REGISTRY} -u "$HARBOR_USER" --password-stdin
                        docker push ${IMAGE}:${TAG}
                        if [ "${PUSH_LATEST}" = "true" ]; then
                            docker tag ${IMAGE}:${TAG} ${IMAGE}:latest
                            docker push ${IMAGE}:latest
                        fi
                        docker logout ${REGISTRY}
                    '''
                }
            }
        }

        stage('Deploy to k8s') {
            when { expression { params.DEPLOY } }
            steps {
                withCredentials([file(credentialsId: 'kubeconfig-u4-2', variable: 'KUBECONFIG')]) {
                    sh '''
                        # kustomization 의 태그를 이번 빌드로 바꾼 뒤 적용한다 (작업 트리만 바꾸고 커밋하지 않는다)
                        cd deploy/k8s
                        kubectl kustomize . >/dev/null
                        (command -v kustomize >/dev/null && kustomize edit set image ${IMAGE}=${IMAGE}:${TAG}) || \
                            sed -i "s#newTag: .*#newTag: ${TAG}#" kustomization.yaml
                        kubectl apply -k . -n ${NAMESPACE}
                        kubectl -n ${NAMESPACE} rollout status deploy/plant-sim --timeout=30m
                        kubectl -n ${NAMESPACE} get pods -l app=plant-sim
                    '''
                }
            }
        }
    }

    post {
        always {
            sh 'docker image rm -f ${IMAGE}:${TAG} >/dev/null 2>&1 || true'
            cleanWs(deleteDirs: true, notFailBuild: true)
        }
        failure {
            echo "빌드 실패: ${env.JOB_NAME} #${env.BUILD_NUMBER} (${env.TAG})"
        }
    }
}
