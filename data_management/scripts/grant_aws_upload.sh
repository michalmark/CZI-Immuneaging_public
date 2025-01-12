#!/bin/bash -e

## This script is a wrapper around AWS CLI for uploading data to the S3 bucket.
## It can be used either for a one-time data upload without AWS access or for uploading general files (i.e. not just fastq files via the upload.py script).
yea and also if we just have data that someone without aws access needs to just quickly upload


if [ $# -ne 2 ]; then
  echo "Usage: $0 <S3_BUCKET> <S3_FOLDER>"
  exit 1
fi

s3_bucket=$1
s3_folder=$2

# Build and run aws command to share the folder
cmd="aws sts get-federation-token --name ro_upload_access --duration-seconds 129600 --policy '{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {
     \"Sid\": \"AllowRootListingOfBCLBucket\",
     \"Action\": [\"s3:ListBucket\"],
     \"Effect\": \"Allow\",
     \"Resource\": [\"arn:aws:s3:::$s3_bucket\"]
   },
   {
     \"Sid\": \"AllowListingOfSpecificFolder\",
     \"Action\": [\"s3:ListBucket\"],
     \"Effect\": \"Allow\",
     \"Resource\": [\"arn:aws:s3:::$s3_bucket/$s3_folder*\"]
   },
   {
     \"Sid\": \"AllowAllS3ActionsInUserFolder\",
     \"Effect\": \"Allow\",
     \"Action\": [\"s3:Get*\", \"s3:Put*\"],
     \"Resource\": [\"arn:aws:s3:::$s3_bucket/$s3_folder*\"]
  }
  ]
}'"


if [ "$verbose" = 1 ]
then
  echo $cmd
fi
output=$(bash <<EOF
$cmd
EOF
)

ACCESSKEY=$(echo "$output" | grep AccessKeyId | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')
SECRETKEY=$(echo "$output" | grep SecretAccessKey | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')
TOKEN=$(echo "$output" | grep SessionToken | awk '{print $2}' | sed 's/"//g' | sed 's/,$//')

echo \# INSTRUCTIONS FOR UPLOADING YOUR FILES
echo \# Ensure that the aws cli is installed: http://docs.aws.amazon.com/cli/latest/userguide/installing.html
echo \# Then run the commands below within the next 36 hours. If you want to just check what the command will do, add the '--dryrun' option. If the sync fails due to a broken connection, you can run it again and it will restart where it left off.
echo \# Mac/Linux instructions:
echo "if [ \$# -ne 1 ]; then"
echo "  echo \"Usage \$0 <FOLDER_TO_UPLOAD> \""
echo "  exit 1"
echo "fi"
echo
echo  export AWS_ACCESS_KEY_ID=$ACCESSKEY
echo  export AWS_SECRET_ACCESS_KEY=$SECRETKEY
echo  export AWS_SESSION_TOKEN=$TOKEN
echo
echo  folder_to_upload=\$1
echo  aws s3 sync \$folder_to_upload s3://$s3_bucket/$s3_folder
echo
echo \# Windows instructions \(comment out the above section and run this instead\):
echo \# SET AWS_ACCESS_KEY_ID=$ACCESSKEY
echo \# SET AWS_SECRET_ACCESS_KEY=$SECRETKEY
echo \# SET AWS_SESSION_TOKEN=$TOKEN
echo \# aws s3 sync \$folder_to_upload s3://$s3_bucket/$s3_folder
echo
