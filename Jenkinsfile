pipeline {
    agent any

    stages {

        stage('Deploy') {
            steps {
                sh '''
                docker stop django_trading_bot || true
                docker rm django_trading_bot || true
                docker rmi my_django_app_image || true

                cd /full/path/AutoTradingBot
                git pull

                docker build -t my_django_app_image .

                cd ..

                docker run -d \
                  --name django_trading_bot \
                  --network algobot-net \
                  --restart unless-stopped \
                  -p 8000:8000 \
                  my_django_app_image
                '''
            }
        }

    }
}