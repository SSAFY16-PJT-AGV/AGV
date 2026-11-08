#include "WarehouseCameraPawn.h"

#include "Camera/CameraComponent.h"
#include "GameFramework/SpringArmComponent.h"
#include "Components/InputComponent.h"
#include "Components/SceneComponent.h"
#include "GameFramework/PlayerController.h"

AWarehouseCameraPawn::AWarehouseCameraPawn()
{
    PrimaryActorTick.bCanEverTick = true;

    SceneRoot = CreateDefaultSubobject<USceneComponent>(TEXT("SceneRoot"));
    RootComponent = SceneRoot;

    SpringArm = CreateDefaultSubobject<USpringArmComponent>(TEXT("SpringArm"));
    SpringArm->SetupAttachment(SceneRoot);
    SpringArm->TargetArmLength = 1200.0f;
    SpringArm->SetRelativeRotation(FRotator(-55.0f, 0.0f, 0.0f));
    SpringArm->bDoCollisionTest = false;

    Camera = CreateDefaultSubobject<UCameraComponent>(TEXT("Camera"));
    Camera->SetupAttachment(SpringArm);

    AutoPossessPlayer = EAutoReceiveInput::Player0;
}

void AWarehouseCameraPawn::BeginPlay()
{
    Super::BeginPlay();

    if (APlayerController* PC = GetWorld()->GetFirstPlayerController())
    {
        PC->bShowMouseCursor = true;

        FInputModeGameAndUI InputMode;
        InputMode.SetHideCursorDuringCapture(false);
        PC->SetInputMode(InputMode);
    }
}

void AWarehouseCameraPawn::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);
}

void AWarehouseCameraPawn::SetupPlayerInputComponent(
    UInputComponent* PlayerInputComponent)
{
    Super::SetupPlayerInputComponent(PlayerInputComponent);

    PlayerInputComponent->BindAxis(
        TEXT("CameraForward"), this, &AWarehouseCameraPawn::MoveForward);

    PlayerInputComponent->BindAxis(
        TEXT("CameraRight"), this, &AWarehouseCameraPawn::MoveRight);

    PlayerInputComponent->BindAxis(
        TEXT("CameraTurn"), this, &AWarehouseCameraPawn::TurnCamera);

    PlayerInputComponent->BindAxis(
        TEXT("CameraLookUp"), this, &AWarehouseCameraPawn::LookUpCamera);

    PlayerInputComponent->BindAxis(
        TEXT("CameraZoom"), this, &AWarehouseCameraPawn::ZoomCamera);

    PlayerInputComponent->BindAction(
        TEXT("CameraRotate"), IE_Pressed,
        this, &AWarehouseCameraPawn::RightMousePressed);

    PlayerInputComponent->BindAction(
        TEXT("CameraRotate"), IE_Released,
        this, &AWarehouseCameraPawn::RightMouseReleased);
}

void AWarehouseCameraPawn::MoveForward(float Value)
{
    if (FMath::IsNearlyZero(Value)) return;

    const FRotator YawRotation(0.0f, GetActorRotation().Yaw, 0.0f);
    AddActorWorldOffset(
        FRotationMatrix(YawRotation).GetUnitAxis(EAxis::X)
        * Value * MoveSpeed * GetWorld()->GetDeltaSeconds());
}

void AWarehouseCameraPawn::MoveRight(float Value)
{
    if (FMath::IsNearlyZero(Value)) return;

    const FRotator YawRotation(0.0f, GetActorRotation().Yaw, 0.0f);
    AddActorWorldOffset(
        FRotationMatrix(YawRotation).GetUnitAxis(EAxis::Y)
        * Value * MoveSpeed * GetWorld()->GetDeltaSeconds());
}

void AWarehouseCameraPawn::TurnCamera(float Value)
{
    if (!bRightMouseDown) return;

    AddActorWorldRotation(
        FRotator(0.0f, Value * RotationSpeed, 0.0f));
}

void AWarehouseCameraPawn::LookUpCamera(float Value)
{
    if (!bRightMouseDown) return;

    FRotator Rotation = SpringArm->GetRelativeRotation();

    Rotation.Pitch = FMath::Clamp(
        Rotation.Pitch + Value * RotationSpeed,
        -85.0f,
        -15.0f
    );

    SpringArm->SetRelativeRotation(Rotation);
}

void AWarehouseCameraPawn::ZoomCamera(float Value)
{
    SpringArm->TargetArmLength = FMath::Clamp(
        SpringArm->TargetArmLength - Value * ZoomSpeed,
        300.0f,
        3000.0f
    );
}

void AWarehouseCameraPawn::RightMousePressed()
{
    bRightMouseDown = true;
}

void AWarehouseCameraPawn::RightMouseReleased()
{
    bRightMouseDown = false;
}